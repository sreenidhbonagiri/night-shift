"""Night Shift incident workflow orchestrator.

Coordinates Sentry → Engineer → QA → (human approval pending).
Records an exportable incident timeline with UTC timestamps and monotonic elapsed time.

Security boundaries:
- Every proposal is re-validated at the orchestration boundary BEFORE any
  installer call: package must be the fixed constant, version must pass the
  shared evaluator's policy. Invalid proposals never reach installation.
- The trusted fault-injection exception allows exactly jinja2==3.1.2 and is
  generated only here by the orchestrator; model proposals cannot activate it.
- Deterministic evidence is recomputed by the shared evaluator
  (app/evidence.py) at the orchestration boundary too — never trusted from
  downstream dicts.
- The patch artifact is persisted per incident (requirements + trusted fixture
  code + tests + manifest) and QA executes that snapshot, not the mutable
  fixture source.
"""
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.sentry_agent import triage
from app.engineer_agent import propose_upgrade
from app.isolated_runner import (
    verify_patch, persist_artifact, REMEDIATION_PACKAGE,
    validate_version, validate_requirements,
)
from app.qa_agent import verify as qa_verify
from app import evidence as evidence_mod

ADVISORY_CONTEXT = (
    "GHSA-h5c8-rqwp-cp95 (CVE-2024-22195) and GHSA-h75v-3vvj-5mfj (CVE-2024-34064): "
    "Jinja2 xmlattr filter allows invalid characters in attribute keys. "
    "GHSA-h5c8-rqwp-cp95 patched in 3.1.3; GHSA-h75v-3vvj-5mfj patched in 3.1.4. "
    "Target remediation: jinja2==3.1.4."
)

MAX_ATTEMPTS = 2


def _validate_proposal(proposal: Optional[dict], trusted_scenario: bool = False) -> Optional[str]:
    """Orchestration-boundary validation. Returns the version string if valid,
    else None.

    Model proposals must match the fixed package AND the normal version
    allowlist. The labeled fault-injection scenario is orchestrator-generated
    trusted code (never model output) and is held to the exact exception:
    version must be exactly the fixed fault-injection version.
    """
    if not isinstance(proposal, dict):
        return None
    if str(proposal.get("package", "")).lower() != REMEDIATION_PACKAGE:
        return None
    return str(proposal.get("version", "")).strip() if validate_version(
        str(proposal.get("version", "")).strip(), fault_injection=trusted_scenario
    ) else None


def _rejection_payload(proposal, verification, qa) -> dict:
    """Current rejection evidence for the Engineer retry prompt, using the
    live pytest_returncode / junit / recomputed deterministic_checks fields."""
    return {
        "proposal": proposal,
        "reason": qa.get("reason"),
        "installed_version": verification.get("installed_version"),
        "proposed_version": verification.get("proposed_version"),
        "pytest_returncode": verification.get("pytest_returncode"),
        "junit": verification.get("junit"),
        "deterministic_checks": evidence_mod.evaluate(verification),
    }


def _artifact_diff(artifact_dir) -> Optional[str]:
    """Read the persisted requirements diff, if present."""
    if not artifact_dir:
        return None
    from pathlib import Path
    p = Path(artifact_dir) / "requirements.diff"
    return p.read_text() if p.is_file() else None


def run_incident(log_key: str = "actionable", fault_inject_first: bool = True,
                 engineer_fn=propose_upgrade, sentry_fn=triage,
                 runner_fn=verify_patch, qa_fn=qa_verify,
                 artifact_dir_root=None,
                 incident_id: Optional[str] = None,
                 progress_cb=None) -> dict:
    """Run one end-to-end incident workflow.

    If fault_inject_first is True, the first attempt is a clearly labeled
    fault-injection scenario proposing the still-affected 3.1.2; QA must
    actually reject it, and Engineer gets one corrected attempt.

    Agent functions are injectable for mock-based tests.

    incident_id: use this pre-registered id instead of generating a new one
    (the /workflow endpoint pre-registers before starting the background job).

    progress_cb: optional callable(events, attempts, status) invoked after
    every recorded event so live progress is published as operations occur,
    rather than only at completion. It receives the incident's current
    event list, attempt list, and status.
    """
    monotonic_start = time.monotonic()
    alert_ts = datetime.now(timezone.utc).isoformat()

    incident: dict = {
        "id": incident_id or str(uuid.uuid4()),
        "log_key": log_key,
        "alert_utc": alert_ts,
        "events": [],
        "attempts": [],
    }

    def _publish(status=None):
        if progress_cb is not None:
            try:
                progress_cb(incident["events"], incident["attempts"],
                           status or incident.get("status"))
            except Exception:
                # Progress publishing must never break the workflow.
                pass

    # --- Sentry triage ---
    sentry = sentry_fn(log_key)
    incident["events"].append({"agent": "sentry", **sentry})
    _publish()
    if sentry.get("disposition") != "ACTIONABLE":
        incident["status"] = "blocked"
        incident["reason"] = f"Sentry disposition is {sentry.get('disposition')}; remediation blocked. No patch attempts created."
        incident["elapsed_alert_to_tested_patch_ms"] = None
        return incident

    # --- Engineer + QA loop (max MAX_ATTEMPTS) ---
    previous_rejection: Optional[dict] = None
    verified_attempt: Optional[dict] = None

    for attempt_num in range(1, MAX_ATTEMPTS + 1):
        # Fault injection: first attempt is deliberately bad if requested.
        # This is an orchestrator-generated scenario, clearly labeled, NOT a
        # model decision.
        if fault_inject_first and attempt_num == 1:
            engineer = {
                "proposal": {"package": "jinja2", "version": "3.1.2",
                             "reason": "FAULT INJECTION: deliberately still-affected version"},
                "reason": "FAULT INJECTION: deliberately still-affected version",
                "model": "system",
                "elapsed_ms": 0.0,
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
                "error": None,
                "fault_injection": True,
                "raw": "FAULT INJECTION: proposal generated by the orchestrator, not a model call.",
            }
        else:
            engineer = engineer_fn(ADVISORY_CONTEXT, previous_rejection)
            engineer["fault_injection"] = False

        # Grounding failure: engineer returns needs_human_review with
        # proposal=None. Stop the loop immediately — no installation,
        # approval, or release can follow.
        if engineer.get("investigation", {}).get("needs_human_review"):
            attempt = {"attempt": attempt_num, "engineer": engineer,
                       "verification": None, "qa": None}
            incident["attempts"].append(attempt)
            incident["events"].append({"agent": "engineer", **engineer})
            incident["events"].append({
                "agent": "workflow", "action": "needs_human_review",
                "reason": engineer.get("reason", "Grounding failed."),
                "utc_timestamp": datetime.now(timezone.utc).isoformat(),
            })
            incident["status"] = "needs_human_review"
            incident["reason"] = engineer.get("reason", "Grounding failed.")
            # Honest timing: no tested patch exists, so alert-to-tested-patch
            # stays null; record time-to-handoff separately.
            incident["elapsed_alert_to_tested_patch_ms"] = None
            incident["elapsed_alert_to_handoff_ms"] = round(
                (time.monotonic() - monotonic_start) * 1000, 2)
            _publish()
            return incident

        proposal = engineer.get("proposal")
        version = _validate_proposal(proposal, trusted_scenario=bool(engineer.get("fault_injection")))

        if version is None:
            # Boundary: invalid proposal must never reach the installer.
            verification = {
                "install_ok": False,
                "version_inspection_ok": False,
                "installed_version": None,
                "proposed_version": proposal.get("version") if isinstance(proposal, dict) else None,
                "pytest_returncode": None,
                "junit": None,
                "required_tests_ok": False,
                "success": False,
                "error": "Proposal rejected at orchestration boundary (package/version not allowlisted); installer never invoked.",
                "test_output": "",
                "artifact_sha256": None,
                "artifact_dir": None,
                "manifest": None,
                "requirements_validated": False,
                "installer_invoked": False,
            }
        else:
            # fault_injection flag only comes from the orchestrator's own
            # labeled scenario; a model attempt can never set it.
            is_fault = bool(engineer.get("fault_injection")) and attempt_num == 1
            artifact = persist_artifact(incident["id"], version,
                                        fault_injection=is_fault,
                                        base_dir=artifact_dir_root)
            if artifact is None:
                verification = {
                    "install_ok": False, "version_inspection_ok": False,
                    "installed_version": None, "proposed_version": version,
                    "pytest_returncode": None, "junit": None,
                    "required_tests_ok": False, "success": False,
                    "error": "Artifact build failed version/requirements validation; installer never invoked.",
                    "test_output": "", "artifact_sha256": None,
                    "artifact_dir": None, "manifest": None,
                    "requirements_validated": False, "installer_invoked": False,
                }
            else:
                verification = runner_fn(artifact)
                verification["proposed_version"] = version
                verification["installer_invoked"] = True

        # --- QA: independent verification over structured evidence ---
        # Recompute at the orchestration boundary too.
        verification["deterministic_checks"] = evidence_mod.evaluate(verification)

        qa = qa_fn(verification)

        attempt = {"attempt": attempt_num, "engineer": engineer,
                   "verification": verification, "qa": qa}
        incident["attempts"].append(attempt)
        incident["events"].append({"agent": "engineer", **engineer})
        incident["events"].append({"agent": "qa", **qa})
        _publish()

        # Success only when both the shared evaluator accepts the execution
        # evidence AND QA approval is strictly True. An incomplete QA object
        # cannot establish success.
        if (evidence_mod.evidence_ok(verification) is True
                and qa.get("approval") is True):
            verified_attempt = attempt
            break

        # Pass the actual rejection evidence to Engineer for the next attempt.
        previous_rejection = _rejection_payload(proposal, verification, qa)

    # --- Honest terminal states ---
    if verified_attempt is not None:
        incident["status"] = "tested_patch_awaiting_human_approval"
        incident["verified_patch"] = {
            "package": verified_attempt["engineer"]["proposal"]["package"],
            "version": verified_attempt["engineer"]["proposal"]["version"],
            "artifact_sha256": verified_attempt["verification"].get("artifact_sha256"),
            "artifact_dir": verified_attempt["verification"].get("artifact_dir"),
            "manifest": verified_attempt["verification"].get("manifest"),
            "requirements_diff": (verified_attempt["verification"].get("artifact")
                                  or {}).get("diff") or _artifact_diff(
                verified_attempt["verification"].get("artifact_dir")),
            "installed_version": verified_attempt["verification"].get("installed_version"),
        }

        # Create a Git proposal branch as a concrete, reviewable deliverable.
        git_ok = False
        git_reason = None
        try:
            from app import git_proposal as gp
            git_prop = gp.create_proposal(
                incident_id=incident["id"],
                version=incident["verified_patch"]["version"],
                artifact_dir=incident["verified_patch"]["artifact_dir"] or "",
                artifact_sha256=incident["verified_patch"]["artifact_sha256"] or "",
                manifest=incident["verified_patch"]["manifest"] or {},
            )
            if git_prop and "error" not in git_prop:
                # QA re-verifies from the actual Git checkout.
                git_verify = gp.verify_checkout(git_prop)
                incident["git_proposal"] = {**git_prop, **git_verify}
                git_ok = bool(git_verify.get("verified"))
                if not git_ok:
                    git_reason = git_verify.get("error") or "Git checkout verification failed"
            else:
                incident["git_proposal"] = git_prop or {"error": "Git proposal not created"}
                git_reason = (git_prop or {}).get("error") or "Git proposal not created"
        except Exception as ex:
            incident["git_proposal"] = {"error": f"Git proposal error: {ex}"}
            git_reason = f"Git proposal error: {ex}"

        if not git_ok:
            # Git delivery is part of this workflow: failed creation or
            # verification blocks the approval-ready state with an honest
            # handoff reason.
            incident["status"] = "remediation_failed"
            incident["elapsed_alert_to_tested_patch_ms"] = None
            incident["reason"] = (
                "QA-verified patch exists but the Git deliverable could not "
                f"be verified: {git_reason}. Human handoff required; no "
                "approval can be granted on an unverified Git proposal."
            )
            _publish()
            return incident

        incident["elapsed_alert_to_tested_patch_ms"] = round(
            (time.monotonic() - monotonic_start) * 1000, 2)
        incident["reason"] = "QA-verified patch awaiting explicit human approval."
        _publish()
    else:
        incident["status"] = "remediation_failed"
        incident["elapsed_alert_to_tested_patch_ms"] = None
        incident["reason"] = (
            f"All {MAX_ATTEMPTS} remediation attempts were rejected by QA. "
            "No verified patch; no approval can be granted."
        )
        _publish()
    return incident
