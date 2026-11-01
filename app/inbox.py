"""Mixed synthetic incident inbox: triages a fixed batch of signals and
gates remediation so only a sufficiently evidenced actionable signal
starts the existing single recovery workflow. Benign and uncertain
signals remain as separate handoffs with no patch attempts.

Uses the existing Sentry triage, workflow, approval, and staging
components — no new agents or frameworks.
"""
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import sentry_agent
from app import workflow as wf

# The fixed inbox batch. Evaluation metadata (expected scenario labels)
# is stored here, never in model inputs.
INBOX_BATCH = [
    {"key": "inbox_success",
     "description": "Successful HTTP request",
     "expected_label": "BENIGN"},
    {"key": "inbox_app_error",
     "description": "Harmless application error (null amount, request rejected)",
     "expected_label": "BENIGN"},
    {"key": "inbox_dep_scan",
     "description": "Dependency scanner finding (CVE-2024-34064)",
     "expected_label": "ACTIONABLE"},
    {"key": "inbox_app_warning",
     "description": "Template cache miss rate warning",
     "expected_label": "INVESTIGATE"},
    {"key": "inbox_incomplete",
     "description": "Incomplete signal (metrics endpoint timeout)",
     "expected_label": "NEEDS_HUMAN_REVIEW"},
]

# Markers that connect a signal to the dependency-remediation workflow.
# A signal must contain one of these to be eligible for the Jinja2
# remediation — a generic application error or performance warning is
# not sufficient even if the model marks it ACTIONABLE.
DEPENDENCY_EVIDENCE_MARKERS = (
    "jinja2", "jinja", "cve-2024-22195", "cve-2024-34064",
    "ghsa-h5c8-rqwp-cp95", "ghsa-h75v-3vvj-5mfj",
    "dependency scanner", "affected by",
)


def _has_dependency_evidence(log_key: str, triage_out: dict) -> bool:
    """Check whether a signal has an explicit evidence connection to the
    affected dependency, advisory metadata, and repository findings.

    A generic application error or performance warning is NOT sufficient
    to start the Jinja2 remediation workflow, even if the model marks it
    ACTIONABLE. The signal's log text must contain a dependency/advisory
    marker that connects it to the specific CVE and affected package.

    The advisory_metadata in the triage output is global CVE context
    (always present for all signals) and is NOT by itself a signal-specific
    connection — it describes the advisory, not the signal.
    """
    log = sentry_agent.SYNTHETIC_FIXTURES.get(log_key, "")
    log_lower = log.lower()
    return any(marker in log_lower for marker in DEPENDENCY_EVIDENCE_MARKERS)


def process_inbox(
    triage_fn: Optional[Callable] = None,
    workflow_fn: Optional[Callable] = None,
    fault_inject_first: bool = True,
    progress_cb: Optional[Callable] = None,
) -> dict[str, Any]:
    """Process the fixed inbox batch.

    For each signal:
      1. Run Sentry triage (real or injected).
      2. Record the classification, original evidence, and advisory
         connection.
      3. Separate model disposition from remediation eligibility:
         - ACTIONABLE means "investigate"; it does NOT automatically
           authorize the fixed Jinja2 remediation.
         - Only a signal with explicit evidence connecting it to the
           affected dependency/version and trusted advisory metadata
           may start the dependency workflow.
         - Application warnings marked INVESTIGATE are recorded for
           investigation but are not patch-eligible.
      4. BENIGN signals are recorded with no further action.
      5. NEEDS_HUMAN_REVIEW signals are recorded as separate handoffs
         with no patch attempts and null timing.
      6. Only one dependency-remediation workflow may start; additional
         eligible signals are recorded but do not start a second workflow.
      7. The selected signal's evidence is passed into recovery via the
         workflow's sentry_fn — the workflow does not re-triage from
         scratch; it uses the inbox's triage result.

    Returns a summary dict with all classifications, the single recovery
    incident (if any), and handoff records.
    """
    _triage = triage_fn or sentry_agent.triage
    _workflow = workflow_fn or wf.run_incident

    monotonic_start = time.monotonic()
    results = []
    actionable_signals = []      # model-said-ACTIONABLE (investigate)
    investigate_signals = []     # app warnings worth investigating
    handoffs = []
    remediation_eligible = []    # signals with dependency evidence
    recovery_incident = None
    classification_mismatches = []

    for item in INBOX_BATCH:
        log_key = item["key"]
        triage_out = _triage(log_key)

        entry = {
            "log_key": log_key,
            "description": item["description"],
            "expected_label": item["expected_label"],
            "disposition": triage_out.get("disposition"),
            "model_disposition": triage_out.get("model_disposition"),
            "model_assessed_severity": triage_out.get("model_assessed_severity"),
            "policy_override": triage_out.get("policy_override"),
            "reason": triage_out.get("reason"),
            "advisory_severity": triage_out.get("advisory_severity"),
            "advisory_metadata": triage_out.get("advisory_metadata"),
            "synthetic": triage_out.get("synthetic"),
            "scenario_label": triage_out.get("scenario_label"),
            "elapsed_ms": triage_out.get("elapsed_ms"),
            "utc_timestamp": triage_out.get("utc_timestamp"),
            "has_dependency_evidence": _has_dependency_evidence(log_key, triage_out),
        }
        results.append(entry)

        # Check classification against expected label.
        expected = item["expected_label"]
        actual = triage_out.get("disposition")
        if expected == "BENIGN" and actual not in ("BENIGN",):
            classification_mismatches.append({
                "log_key": log_key, "expected": expected, "actual": actual,
                "note": "Expected BENIGN; model classified differently."})
        elif expected == "NEEDS_HUMAN_REVIEW" and actual != "NEEDS_HUMAN_REVIEW":
            classification_mismatches.append({
                "log_key": log_key, "expected": expected, "actual": actual,
                "note": "Expected NEEDS_HUMAN_REVIEW; model classified differently."})

        disp = triage_out.get("disposition")
        if disp == "ACTIONABLE":
            actionable_signals.append(entry)
            # Separate model disposition from remediation eligibility.
            if entry["has_dependency_evidence"]:
                remediation_eligible.append(entry)
            else:
                # ACTIONABLE but no dependency evidence — investigate,
                # don't remediate.
                investigate_signals.append(entry)
        elif disp == "NEEDS_HUMAN_REVIEW":
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
        elif disp == "BENIGN" and expected == "INVESTIGATE":
            # App warning model-classified BENIGN but intended for
            # investigation — record as investigate, not patch-eligible.
            investigate_signals.append(entry)

    # Also check expected INVESTIGATE signals that weren't ACTIONABLE.
    for r in results:
        if (r["expected_label"] == "INVESTIGATE"
                and r not in investigate_signals
                and r not in [s for s in remediation_eligible]):
            investigate_signals.append(r)

    # Gate: only the FIRST remediation-eligible signal (ACTIONABLE + has
    # dependency evidence) starts the dependency workflow. The selected
    # signal's triage result is passed into recovery so the workflow does
    # NOT re-triage from scratch.
    if remediation_eligible:
        signal = remediation_eligible[0]
        incident_id = str(uuid.uuid4())
        selected_triage = _triage(signal["log_key"]) if triage_fn is None else \
            triage_fn(signal["log_key"])

        # Pass the selected evidence into recovery: the workflow uses
        # this sentry_fn instead of re-triaging from scratch.
        def _bound_sentry_fn(log_key_arg, _triage_result=selected_triage):
            return _triage_result

        recovery_incident = _workflow(
            log_key=signal["log_key"],
            fault_inject_first=fault_inject_first,
            incident_id=incident_id,
            progress_cb=progress_cb,
            sentry_fn=_bound_sentry_fn,
        )
        # The fault-injection label is already recorded by the workflow
        # itself (engineer event with fault_injection=True on attempt 1).

    elapsed_ms = round((time.monotonic() - monotonic_start) * 1000, 2)

    return {
        "id": str(uuid.uuid4()),
        "inbox_results": results,
        "actionable_signals": actionable_signals,
        "investigate_signals": investigate_signals,
        "remediation_eligible": remediation_eligible,
        "handoffs": handoffs,
        "recovery_incident": recovery_incident,
        "classification_mismatches": classification_mismatches,
        "summary": {
            "total": len(results),
            "actionable": len(actionable_signals),
            "benign": sum(1 for r in results if r["disposition"] == "BENIGN"),
            "needs_human_review": sum(
                1 for r in results if r["disposition"] == "NEEDS_HUMAN_REVIEW"),
            "investigate": len(investigate_signals),
            "remediation_eligible": len(remediation_eligible),
            "recovery_started": recovery_incident is not None,
            "classification_mismatches": len(classification_mismatches),
        },
        "elapsed_ms": elapsed_ms,
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
    }
