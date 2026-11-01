"""Mixed synthetic incident inbox: triages a fixed batch of signals and
gates remediation so only a sufficiently evidenced actionable signal
starts the existing single recovery workflow. Benign and uncertain
signals remain as separate handoffs with no patch attempts.

Evaluation dimensions are kept separate:
  - Model disposition (what Sentry concluded): BENIGN, ACTIONABLE,
    NEEDS_HUMAN_REVIEW.
  - Policy route (what the system does about it): dismiss, investigate,
    human_review, remediate.
These are different enums and are never compared to each other.

Uses the existing Sentry triage, workflow, approval, and staging
components — no new agents or frameworks.
"""
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import sentry_agent
from app import workflow as wf
from app.engineer_agent import propose_upgrade

# The fixed inbox batch. Evaluation metadata (expected dispositions and
# routes) is stored here, never in model inputs.
INBOX_BATCH = [
    {"key": "inbox_success",
     "description": "Successful HTTP request",
     "expected_disposition": "BENIGN", "expected_route": "dismiss"},
    {"key": "inbox_app_error",
     "description": "Harmless application error (null amount, request rejected)",
     "expected_disposition": "BENIGN", "expected_route": "dismiss"},
    {"key": "inbox_dep_scan",
     "description": "Dependency scanner finding (CVE-2024-34064)",
     "expected_disposition": "ACTIONABLE", "expected_route": "remediate"},
    {"key": "inbox_app_warning",
     "description": "Template cache miss rate warning",
     "expected_disposition": "ACTIONABLE", "expected_route": "investigate"},
    {"key": "inbox_incomplete",
     "description": "Incomplete signal (metrics endpoint timeout)",
     "expected_disposition": "NEEDS_HUMAN_REVIEW",
     "expected_route": "human_review"},
]

# Patch eligibility requires ALL of: a supported package, a concrete
# affected version, and a matching trusted advisory. Generic markers
# such as "affected by" or "dependency scanner" alone are NOT sufficient
# to authorize Jinja2 remediation.
SUPPORTED_PACKAGES = ("jinja2",)
TRUSTED_ADVISORIES = {
    "cve-2024-22195": "jinja2",
    "cve-2024-34064": "jinja2",
    "ghsa-h5c8-rqwp-cp95": "jinja2",
    "ghsa-h75v-3vvj-5mfj": "jinja2",
}
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")


def _patch_eligibility(log_text: str) -> dict:
    """Check whether a signal carries the specific evidence required to
    authorize the fixed dependency remediation: a supported package, a
    concrete affected version, and a matching trusted advisory.

    Repository investigation (Engineer grounding) and QA still validate
    the proposed change afterwards; this gate only decides whether the
    dependency workflow may start at all.
    """
    text = log_text or ""
    lower = text.lower()
    package = next((p for p in SUPPORTED_PACKAGES if p in lower), None)
    advisory = next((a for a in TRUSTED_ADVISORIES if a in lower), None)
    version_match = _VERSION_RE.search(text)

    missing = []
    if not package:
        missing.append("supported package")
    if not version_match:
        missing.append("concrete affected version")
    if not advisory:
        missing.append("matching trusted advisory")
    elif package and TRUSTED_ADVISORIES[advisory] != package:
        missing.append("advisory does not match the supported package")

    return {
        "eligible": not missing,
        "package": package,
        "version": version_match.group(0) if version_match else None,
        "advisory": advisory,
        "missing": missing,
    }


def _route_for(disposition: Optional[str], eligibility: dict) -> str:
    """Map a model disposition to a policy route.

    BENIGN -> dismiss. NEEDS_HUMAN_REVIEW -> human_review.
    ACTIONABLE -> remediate only when the signal is patch-eligible
    (supported package + concrete version + matching trusted advisory);
    otherwise investigate.
    """
    if disposition == "BENIGN":
        return "dismiss"
    if disposition == "ACTIONABLE":
        return "remediate" if eligibility.get("eligible") else "investigate"
    # NEEDS_HUMAN_REVIEW and anything unknown escalate to a human.
    return "human_review"


def process_inbox(
    triage_fn: Optional[Callable] = None,
    workflow_fn: Optional[Callable] = None,
    engineer_fn: Optional[Callable] = None,
    fault_inject_first: bool = True,
    progress_cb: Optional[Callable] = None,
) -> dict[str, Any]:
    """Process the fixed inbox batch.

    For each signal:
      1. Run Sentry triage (real or injected).
      2. Record the model disposition AND the policy route separately.
      3. Route: dismiss (benign), investigate (actionable without patch
         eligibility), human_review (uncertain — separate handoff with
         zero attempts and null timing), remediate (actionable AND
         patch-eligible: supported package + concrete version +
         matching trusted advisory).
      4. Only one dependency-remediation workflow may start.
      5. The selected signal's evidence — ID, original log, triage
         rationale, and advisory context — is passed into recovery by
         binding both sentry_fn (workflow record) and engineer_fn
         (Engineer's actual input context).

    Returns a summary dict with all classifications, routing, the single
    recovery incident (if any), and handoff records.
    """
    _triage = triage_fn or sentry_agent.triage
    _workflow = workflow_fn or wf.run_incident
    _engineer = engineer_fn or propose_upgrade

    monotonic_start = time.monotonic()
    results = []
    investigate_signals = []
    handoffs = []
    remediation_eligible = []
    recovery_incident = None
    classification_mismatches = []
    routing_mismatches = []
    triage_results = {}

    for item in INBOX_BATCH:
        log_key = item["key"]
        triage_out = _triage(log_key)
        triage_results[log_key] = triage_out

        eligibility = _patch_eligibility(triage_out.get("log", ""))
        disposition = triage_out.get("disposition")
        route = _route_for(disposition, eligibility)

        entry = {
            "log_key": log_key,
            "description": item["description"],
            "expected_disposition": item["expected_disposition"],
            "expected_route": item["expected_route"],
            "disposition": disposition,
            "policy_route": route,
            "model_disposition": triage_out.get("model_disposition"),
            "model_assessed_severity": triage_out.get("model_assessed_severity"),
            "policy_override": triage_out.get("policy_override"),
            "reason": triage_out.get("reason"),
            "advisory_severity": triage_out.get("advisory_severity"),
            "advisory_metadata": triage_out.get("advisory_metadata"),
            "synthetic": triage_out.get("synthetic"),
            "elapsed_ms": triage_out.get("elapsed_ms"),
            "utc_timestamp": triage_out.get("utc_timestamp"),
            "patch_eligibility": eligibility,
        }
        results.append(entry)

        # Classification accuracy: model disposition vs expected
        # disposition (same enum).
        if disposition != item["expected_disposition"]:
            classification_mismatches.append({
                "log_key": log_key,
                "expected": item["expected_disposition"],
                "actual": disposition,
                "note": (f"Expected model disposition "
                         f"{item['expected_disposition']}; got {disposition}."),
            })
        # Routing correctness: policy route vs expected route (same enum).
        if route != item["expected_route"]:
            routing_mismatches.append({
                "log_key": log_key,
                "expected": item["expected_route"],
                "actual": route,
                "note": (f"Expected policy route {item['expected_route']}; "
                         f"got {route}."),
            })

        if route == "human_review":
            handoffs.append({
                "id": str(uuid.uuid4()),
                "log_key": log_key,
                "description": item["description"],
                "status": "needs_human_review",
                "attempts": [],
                "elapsed_alert_to_tested_patch_ms": None,
                "reason": triage_out.get("reason"),
                "policy_override": triage_out.get("policy_override"),
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
            })
        elif route == "investigate":
            investigate_signals.append(entry)
        elif route == "remediate":
            remediation_eligible.append(entry)
        # route == "dismiss": recorded in results, no further action.

    # Gate: only the FIRST patch-eligible signal starts the dependency
    # workflow. The selected signal's evidence is passed into recovery by
    # binding sentry_fn (so the workflow records the inbox's triage
    # result, not a re-triage) and engineer_fn (so Engineer's input
    # context carries the selected signal's ID, original log, triage
    # rationale, and advisory context).
    if remediation_eligible:
        signal = remediation_eligible[0]
        selected_triage = triage_results[signal["log_key"]]
        incident_id = str(uuid.uuid4())

        def _bound_sentry_fn(log_key_arg, _triage_result=selected_triage):
            return _triage_result

        def _bound_engineer_fn(advisory_context, previous_rejection=None,
                               _signal=signal, _triage_result=selected_triage):
            selected_context = (
                "SELECTED SIGNAL FROM MIXED INBOX\n"
                f"Signal: {_signal['log_key']} — {_signal['description']}\n"
                f"Original log: {_triage_result.get('log', '')}\n"
                f"Sentry disposition: {_triage_result.get('disposition')}\n"
                f"Sentry rationale: {_triage_result.get('reason', '')}\n"
                "Patch eligibility: "
                f"{_signal['patch_eligibility']['package']} "
                f"{_signal['patch_eligibility']['version']} "
                f"({_signal['patch_eligibility']['advisory']})\n"
                "\nRELEVANT ADVISORY CONTEXT\n"
                + (advisory_context or "")
            )
            return _engineer(selected_context, previous_rejection)

        recovery_incident = _workflow(
            log_key=signal["log_key"],
            fault_inject_first=fault_inject_first,
            incident_id=incident_id,
            progress_cb=progress_cb,
            sentry_fn=_bound_sentry_fn,
            engineer_fn=_bound_engineer_fn,
        )
        # The fault-injection label is recorded by the workflow itself
        # (engineer event with fault_injection=True on attempt 1).

    elapsed_ms = round((time.monotonic() - monotonic_start) * 1000, 2)

    return {
        "id": str(uuid.uuid4()),
        "inbox_results": results,
        "investigate_signals": investigate_signals,
        "remediation_eligible": remediation_eligible,
        "handoffs": handoffs,
        "recovery_incident": recovery_incident,
        "classification_mismatches": classification_mismatches,
        "routing_mismatches": routing_mismatches,
        "summary": {
            "total": len(results),
            "dispositions": {
                "BENIGN": sum(1 for r in results if r["disposition"] == "BENIGN"),
                "ACTIONABLE": sum(1 for r in results
                                  if r["disposition"] == "ACTIONABLE"),
                "NEEDS_HUMAN_REVIEW": sum(
                    1 for r in results
                    if r["disposition"] == "NEEDS_HUMAN_REVIEW"),
            },
            "routes": {
                "dismiss": sum(1 for r in results if r["policy_route"] == "dismiss"),
                "investigate": sum(1 for r in results
                                   if r["policy_route"] == "investigate"),
                "human_review": sum(1 for r in results
                                    if r["policy_route"] == "human_review"),
                "remediate": sum(1 for r in results
                                 if r["policy_route"] == "remediate"),
            },
            "recovery_started": recovery_incident is not None,
            "classification_mismatches": len(classification_mismatches),
            "routing_mismatches": len(routing_mismatches),
        },
        "elapsed_ms": elapsed_ms,
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
    }
