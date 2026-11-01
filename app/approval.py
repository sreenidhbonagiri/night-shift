"""Human approval and simulated deployment gates for Night Shift.

Approval is application-level, incident-specific, and non-transferable.
Approval and simulated release both recompute the artifact manifest from the
actual bytes on disk and compare it with the verified manifest — missing or
changed files fail closed. Stored hash strings alone are never trusted.
Deterministic evidence is recomputed by the shared evaluator, not taken from
the QA-provided checks map.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from app.isolated_runner import recompute_manifest, manifest_matches
from app import evidence as evidence_mod


def _current_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_verified_patch(incident: dict) -> Optional[dict]:
    """Return the QA-approved attempt in the incident, or None."""
    for attempt in reversed(incident.get("attempts", [])):
        if attempt.get("qa", {}).get("approval") is True:
            return attempt
    return None


def _artifact_gate(incident: dict) -> Optional[str]:
    """Recompute the manifest from disk and compare with the verified one.

    Returns None if the artifact is intact, else a reason string.
    """
    bound = incident.get("verified_patch") or {}
    verified_manifest = bound.get("manifest")
    artifact_dir = bound.get("artifact_dir")
    if not artifact_dir:
        return "verified patch has no artifact directory recorded"
    current = recompute_manifest(artifact_dir)
    return manifest_matches(current, verified_manifest)


def approve(incident: dict, actor: str = "human") -> dict:
    """Record human approval for a specific verified incident, patch, and artifact.

    Rejects approval if:
      - incident is missing
      - incident has already been approved (non-transferable)
      - incident status is not tested_patch_awaiting_human_approval
      - no QA-approved verified patch exists
      - any recomputed deterministic evidence check fails
      - proposal version, installed version, or artifact hash are inconsistent
      - the persisted artifact's manifest, recomputed from disk, does not
        match the verified manifest (missing or changed files)
    """
    if not incident:
        return {"approved": False, "error": "No incident provided.", "utc_timestamp": _current_ts()}

    if incident.get("human_approval"):
        return {
            "approved": False,
            "error": "This incident already has a human approval. Approvals are non-transferable.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    if incident.get("status") != "tested_patch_awaiting_human_approval":
        return {
            "approved": False,
            "error": (
                f"Incident status is {incident.get('status')!r}; "
                "approval is only allowed for a genuinely QA-verified patch."
            ),
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    verified = _find_verified_patch(incident)
    if not verified:
        return {
            "approved": False,
            "error": "No QA-approved patch found for this incident.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    # Recompute evidence checks from execution evidence at the approval
    # boundary — never trust the QA-provided checks map.
    verification = verified.get("verification", {})
    failed = evidence_mod.failed_check_names(verification)
    if verified.get("qa", {}).get("approval") is not True or failed:
        return {
            "approved": False,
            "error": (
                "Recomputed deterministic evidence checks did not all pass; cannot approve. "
                f"Failed: {failed}"
            ),
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    proposal = verified.get("engineer", {}).get("proposal", {})
    installed = verification.get("installed_version")
    artifact_sha = verification.get("artifact_sha256")
    bound = incident.get("verified_patch", {})

    if (proposal.get("version") != installed
            or installed != bound.get("version")
            or not artifact_sha
            or artifact_sha != bound.get("artifact_sha256")):
        return {
            "approved": False,
            "error": (
                f"Patch binding mismatch: proposal={proposal.get('version')}, "
                f"installed={installed}, bound={bound.get('version')}, "
                f"artifact_sha256={'present' if artifact_sha else 'missing'}. "
                "Approval requires a consistent, hash-bound verified patch."
            ),
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    # Artifact gate: recompute the manifest from disk and compare.
    manifest_reason = _artifact_gate(incident)
    if manifest_reason:
        return {
            "approved": False,
            "error": f"Artifact manifest recheck failed: {manifest_reason}",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    approval_record = {
        "approved": True,
        "actor": actor,
        "incident_id": incident.get("id"),
        "patch": {
            "package": proposal["package"],
            "version": proposal["version"],
        },
        "artifact_sha256": artifact_sha,
        "manifest_recheck": "passed",
        "utc_timestamp": _current_ts(),
        "note": "Human approval recorded; bound to this incident, patch, and artifact manifest only.",
    }
    incident["human_approval"] = approval_record
    incident["status"] = "human_approved"
    incident["events"].append({"agent": "human", "action": "approval", **approval_record})
    return approval_record


def simulate_deploy(incident: dict) -> dict:
    """Record a simulated deployment action, gated on explicit human approval.

    The artifact manifest is recomputed from disk and compared with the
    verified manifest before recording the simulated release; missing or
    changed files prohibit the release. No deployment commands run and no
    external infrastructure is contacted.
    """
    if not incident:
        return {"simulated": False, "error": "No incident provided.", "utc_timestamp": _current_ts()}

    if not incident.get("human_approval"):
        return {
            "simulated": False,
            "error": "Simulated deployment requires explicit human approval first.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    if incident.get("simulated_deployment"):
        return {
            "simulated": False,
            "error": "Simulated deployment already recorded for this incident.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    approval = incident.get("human_approval", {})
    verified = _find_verified_patch(incident)
    bound = incident.get("verified_patch", {})
    current_sha = verified.get("verification", {}).get("artifact_sha256") if verified else None
    current_version = verified.get("engineer", {}).get("proposal", {}).get("version") if verified else None

    # Recheck artifact hash and version before any simulated release.
    if (approval.get("artifact_sha256") != current_sha
            or approval.get("patch", {}).get("version") != current_version
            or bound.get("artifact_sha256") != current_sha
            or bound.get("version") != current_version):
        return {
            "simulated": False,
            "error": (
                f"Artifact recheck failed: approved hash {approval.get('artifact_sha256')} / "
                f"current hash {current_sha}; approved version "
                f"{approval.get('patch', {}).get('version')} / current version {current_version}. "
                "The verified patch changed after approval; release is prohibited."
            ),
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    # Artifact gate: recompute the manifest from disk again at release time.
    manifest_reason = _artifact_gate(incident)
    if manifest_reason:
        return {
            "simulated": False,
            "error": f"Artifact manifest recheck failed at release time: {manifest_reason}",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }

    sim_record = {
        "simulated": True,
        "incident_id": incident.get("id"),
        "patch": approval.get("patch"),
        "artifact_sha256": approval.get("artifact_sha256"),
        "manifest_recheck": "passed",
        "utc_timestamp": _current_ts(),
        "disclaimer": "SIMULATION ONLY: no external infrastructure contacted, no deployment commands executed.",
    }
    incident["simulated_deployment"] = sim_record
    incident["status"] = "simulated_deployed"
    incident["events"].append({"agent": "deployment_simulator", "action": "simulate_deploy", **sim_record})
    return sim_record
