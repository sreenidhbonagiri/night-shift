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
     "expected_label": "BENIGN_OR_ACTIONABLE"},
    {"key": "inbox_dep_scan",
     "description": "Dependency scanner finding (CVE-2024-34064)",
     "expected_label": "ACTIONABLE"},
    {"key": "inbox_app_warning",
     "description": "Template cache miss rate warning",
     "expected_label": "BENIGN_OR_ACTIONABLE"},
    {"key": "inbox_incomplete",
     "description": "Incomplete signal (metrics endpoint timeout)",
     "expected_label": "NEEDS_HUMAN_REVIEW"},
]


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
      3. If ACTIONABLE with sufficient evidence, gate remediation through
         the existing single recovery workflow. Only one actionable signal
         may start remediation; any second actionable signal is recorded
         but does NOT start a second workflow.
      4. BENIGN signals are recorded with no further action.
      5. NEEDS_HUMAN_REVIEW signals are recorded as separate handoffs
         with no patch attempts and null timing.

    Returns a summary dict with all classifications, the single recovery
    incident (if any), and handoff records.
    """
    _triage = triage_fn or sentry_agent.triage
    _workflow = workflow_fn or wf.run_incident

    monotonic_start = time.monotonic()
    results = []
    actionable_signals = []
    handoffs = []
    recovery_incident = None

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
        }
        results.append(entry)

        disp = triage_out.get("disposition")
        if disp == "ACTIONABLE":
            actionable_signals.append(entry)
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

    # Gate: only the FIRST sufficiently evidenced actionable signal starts
    # the existing single recovery workflow. Any additional actionable
    # signals are recorded but do not start a second workflow.
    if actionable_signals:
        signal = actionable_signals[0]
        incident_id = str(uuid.uuid4())
        recovery_incident = _workflow(
            log_key=signal["log_key"],
            fault_inject_first=fault_inject_first,
            incident_id=incident_id,
            progress_cb=progress_cb,
        )
        # The fault-injection label is already recorded by the workflow
        # itself (engineer event with fault_injection=True on attempt 1).

    elapsed_ms = round((time.monotonic() - monotonic_start) * 1000, 2)

    return {
        "id": str(uuid.uuid4()),
        "inbox_results": results,
        "actionable_signals": actionable_signals,
        "handoffs": handoffs,
        "recovery_incident": recovery_incident,
        "summary": {
            "total": len(results),
            "actionable": len(actionable_signals),
            "benign": sum(1 for r in results if r["disposition"] == "BENIGN"),
            "needs_human_review": sum(
                1 for r in results if r["disposition"] == "NEEDS_HUMAN_REVIEW"),
            "recovery_started": recovery_incident is not None,
        },
        "elapsed_ms": elapsed_ms,
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
    }
