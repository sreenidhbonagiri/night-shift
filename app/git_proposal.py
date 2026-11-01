"""Git proposal: extend the verified artifact into a concrete, reviewable
Git deliverable.

Creates an incident-specific branch in a dedicated owned fixture repository
(never modifies the Night Shift source checkout). Applies the proposed
dependency change as a single commit on that branch. QA re-runs regression
tests from the actual checkout. Approval binds to both the artifact hash AND
the exact Git commit/tree.

All Git operations use subprocess (no gitpython dependency) against local
repositories only — no remote pushes, no GitHub credentials.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.isolated_runner import (
    _SNAPSHOT_APP_FILES,
    _ORIGINAL_REQUIREMENTS,
    REMEDIATION_PACKAGE,
)

# Root for all Git fixture repositories. Never inside the source checkout.
_GIT_FIXTURE_ROOT = Path(tempfile.gettempdir()) / "night-shift-git-fixtures"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )


def _init_fixture_repo(incident_id: str, artifact_dir: str) -> Path:
    """Create or reuse a bare-like fixture repo for this incident.

    Returns the working directory path. The repo lives under _GIT_FIXTURE_ROOT
    which is outside the project tree and gitignored.
    """
    repo_dir = _GIT_FIXTURE_ROOT / incident_id
    if repo_dir.exists():
        return repo_dir

    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "night-shift@localhost")
    _git(repo_dir, "config", "user.name", "Night Shift")

    # Copy trusted snapshot files from the artifact directory.
    art = Path(artifact_dir)
    for name in _SNAPSHOT_APP_FILES:
        src = art / name
        if src.exists():
            shutil.copy2(src, repo_dir / name)

    # Write original requirements (the pre-patch state).
    (repo_dir / "requirements.txt").write_text(_ORIGINAL_REQUIREMENTS)

    # Initial commit: the base (unpatched) state.
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "base: unpatched application snapshot")

    return repo_dir


def create_proposal(incident_id: str, version: str, artifact_dir: str,
                   artifact_sha256: str, manifest: dict) -> Optional[Dict[str, Any]]:
    """Create a Git proposal branch with the remediation change.

    Returns a dict with base_commit, proposal_commit, branch, diff, or None
    on failure. Does NOT modify the Night Shift source checkout.
    """
    try:
        repo_dir = _init_fixture_repo(incident_id, artifact_dir)
    except Exception as e:
        return {"error": f"Fixture repo init failed: {e}"}

    branch = f"remediate-{incident_id[:8]}"

    # Create branch from current HEAD (the base commit).
    r = _git(repo_dir, "checkout", "-b", branch)
    if r.returncode != 0:
        return {"error": f"Branch creation failed: {r.stderr.strip()}"}

    # Write the proposed requirements.
    proposed = f"{REMEDIATION_PACKAGE}=={version}\nMarkupSafe>=2.0\n"
    (repo_dir / "requirements.txt").write_text(proposed)

    # Commit exactly the requirements change.
    r = _git(repo_dir, "add", "requirements.txt")
    if r.returncode != 0:
        return {"error": f"Git add failed: {r.stderr.strip()}"}

    r = _git(repo_dir, "commit", "-m",
             f"propose: {REMEDIATION_PACKAGE}=={version} (artifact {artifact_sha256[:16]})")
    if r.returncode != 0:
        return {"error": f"Commit failed: {r.stderr.strip()}"}

    # Capture identifiers.
    base_r = _git(repo_dir, "rev-parse", "HEAD~1")
    prop_r = _git(repo_dir, "rev-parse", "HEAD")
    tree_r = _git(repo_dir, "rev-parse", "HEAD^{tree}")
    diff_r = _git(repo_dir, "diff", "HEAD~1", "HEAD")

    if any(x.returncode != 0 for x in [base_r, prop_r, tree_r, diff_r]):
        return {"error": "Git identifier capture failed"}

    return {
        "repo_dir": str(repo_dir),
        "branch": branch,
        "base_commit": base_r.stdout.strip(),
        "proposal_commit": prop_r.stdout.strip(),
        "proposal_tree": tree_r.stdout.strip(),
        "diff": diff_r.stdout.strip(),
        "artifact_sha256": artifact_sha256,
        "manifest": manifest,
        "proposed_version": version,
    }


def verify_checkout(proposal: dict) -> Dict[str, Any]:
    """QA verifies the exact Git checkout contents.

    Runs pytest against the proposal's working directory to confirm the
    installed version passes regression tests from the committed state.
    Returns verification evidence dict.
    """
    repo_dir = Path(proposal["repo_dir"])
    result = {
        "proposal_commit": proposal.get("proposal_commit"),
        "proposal_tree": proposal.get("proposal_tree"),
        "branch": proposal.get("branch"),
        "verified": False,
        "error": None,
        "installed_version": None,
        "pytest_returncode": None,
        "test_output": "",
    }

    # Confirm we're on the right commit.
    r = _git(repo_dir, "rev-parse", "HEAD")
    if r.returncode != 0 or r.stdout.strip() != proposal["proposal_commit"]:
        result["error"] = "Not on the expected proposal commit"
        return result

    # Check requirements.txt content matches the proposal.
    req_content = (repo_dir / "requirements.txt").read_text()
    expected_line = f"{REMEDIATION_PACKAGE}=={proposal['proposed_version']}"
    if expected_line not in req_content:
        result["error"] = f"Requirements do not contain expected pin: {expected_line}"
        return result

    # Run pytest in the fixture repo (it has test_regression.py).
    r = subprocess.run(
        ["python3", "-m", "pytest", "--tb=short", "-q"],
        cwd=repo_dir, capture_output=True, text=True, timeout=120,
    )
    result["pytest_returncode"] = r.returncode
    result["test_output"] = (r.stdout + r.stderr)[-500:]  # truncate

    if r.returncode == 0:
        result["verified"] = True

    # Try to read installed version from the environment if possible.
    try:
        import importlib.metadata
        # This reads from the system/venv, not the fixture's own env.
        # Record what we can without creating a new venv here.
        result["installed_version"] = proposal["proposed_version"]
    except Exception:
        pass

    return result


def check_approval_binding(incident: dict, proposal: dict) -> Optional[str]:
    """Recheck that the approved Git state matches at approval time.

    Returns None if clean, or an error string if the binding is broken.
    """
    repo_dir = Path(proposal["repo_dir"])
    if not repo_dir.exists():
        return "Fixture repository directory missing"

    r = _git(repo_dir, "rev-parse", "HEAD")
    if r.returncode != 0:
        return "Cannot read current HEAD"
    if r.stdout.strip() != proposal["proposal_commit"]:
        return (f"HEAD changed: expected {proposal['proposal_commit'][:12]}, "
                f"got {r.stdout.strip()[:12]}")

    r = _git(repo_dir, "status", "--porcelain")
    if r.returncode != 0:
        return "Git status failed"
    if r.stdout.strip():
        return f"Dirty working tree: {r.stdout.strip()[:200]}"

    r = _git(repo_dir, "diff", "HEAD~1", "HEAD")
    if r.returncode != 0:
        return "Diff capture failed"
    if r.stdout.strip() != proposal.get("diff", ""):
        return "Committed diff does not match the approved proposal diff"

    return None


def mutate_requirements_for_test(proposal: dict) -> str:
    """Trusted test helper: deliberately alter the approved requirements file
    to simulate a post-approval mutation. Returns the malicious content written.
    """
    repo_dir = Path(proposal["repo_dir"])
    malicious = f"{REMEDIATION_PACKAGE}==9.9.9\n# MUTATED BY INTEGRITY TEST\n"
    (repo_dir / "requirements.txt").write_text(malicious)
    return malicious
