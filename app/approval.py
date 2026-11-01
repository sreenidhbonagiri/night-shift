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
    """Return the QA-approved attempt in the incident, or None.

    Attempts with missing (None) verification or qa evidence are never
    treated as verified.
    """
    for attempt in reversed(incident.get("attempts", [])):
        qa = attempt.get("qa")
        verification = attempt.get("verification")
        if not isinstance(qa, dict) or not isinstance(verification, dict):
            continue
        if qa.get("approval") is True:
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

    # Git binding: when Git delivery is part of the workflow, the verified
    # checkout must pass verification and its exact commit/tree is recorded
    # in the approval record. Staging rechecks against these values.
    git_prop = incident.get("git_proposal")
    git_binding = None
    if isinstance(git_prop, dict) and git_prop.get("repo_dir"):
        # The workflow stores verification fields flattened into the
        # descriptor ({**proposal, **verification}); accept either shape.
        verification = git_prop.get("verification") or git_prop
        if not verification.get("verified"):
            return {
                "approved": False,
                "error": (
                    "Git proposal verification failed or missing; cannot "
                    f"approve. Detail: {verification.get('error') or git_prop.get('error')}"
                ),
                "incident_id": incident.get("id"),
                "utc_timestamp": _current_ts(),
            }
        from app import git_proposal as gp
        binding_reason = gp.validate_proposal_pre_approval(git_prop)
        if binding_reason:
            return {
                "approved": False,
                "error": f"Git proposal binding failed at approval: {binding_reason}",
                "incident_id": incident.get("id"),
                "utc_timestamp": _current_ts(),
            }
        git_binding = {
            "commit": git_prop.get("proposal_commit"),
            "tree": git_prop.get("proposal_tree"),
            "branch": git_prop.get("branch"),
            "verified": True,
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
    if git_binding:
        approval_record["git"] = git_binding
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
    if approval.get("approved") is not True:
        return {
            "simulated": False,
            "error": "Stored approval record is not strictly approved; release prohibited.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }
    if approval.get("incident_id") != incident.get("id"):
        return {
            "simulated": False,
            "error": (
                f"Approval is bound to incident {approval.get('incident_id')!r}, "
                f"not this incident ({incident.get('id')!r}); approvals are "
                "non-transferable and release is prohibited."
            ),
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }
    verified = _find_verified_patch(incident)
    if verified is None:
        return {
            "simulated": False,
            "error": "No attempt with complete verification and QA evidence; release prohibited.",
            "incident_id": incident.get("id"),
            "utc_timestamp": _current_ts(),
        }
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
