"""Git proposal: extend the verified artifact into a concrete, reviewable
Git deliverable.

Creates an incident-specific branch in a dedicated owned fixture repository
(never modifies the Night Shift source checkout). Commits the artifact's
EXACT requirement bytes and the app snapshot QA actually tests. Verification
builds a disposable venv from the committed requirements and measures
installed versions from that environment — reusing the strict evidence
rules of app/isolated_runner.verify_patch (JUnit, zero failed/errored/
skipped, required tests passed).

Approval binds to the exact commit/tree; staging rechecks against the
approval record. All Git operations are local subprocess calls — no remote
pushes, no credentials.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from app.isolated_runner import (
    _SNAPSHOT_APP_FILES,
    REMEDIATION_PACKAGE,
    SUPPORTING_PINS,
    REQUIRED_TESTS,
    VERIFICATION_TOOL_PIN,
    _run,
    _parse_junit,
    validate_requirements,
)

# Root for all Git fixture repositories. Never inside the source checkout.
_GIT_FIXTURE_ROOT = Path(tempfile.gettempdir()) / "night-shift-git-fixtures"

# Files the committed tree must contain for QA to run at all.
_REQUIRED_COMMITTED_FILES = ("requirements.txt",) + tuple(_SNAPSHOT_APP_FILES)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + list(args), cwd=cwd,
        capture_output=True, text=True, timeout=30,
    )


def _init_fixture_repo(incident_id: str, artifact_dir: str) -> Path:
    """Create the fixture repo for this incident from the artifact snapshot.

    Layout: the artifact persists app files under <art>/app/<name>; the
    committed tree holds them at the repo root, alongside requirements.txt
    (the artifact's exact original requirements bytes).
    """
    art = Path(artifact_dir)
    missing = [n for n in ("requirements-original.txt",
                           "requirements-proposed.txt")
               if not (art / n).is_file()]
    missing += [f"app/{n}" for n in _SNAPSHOT_APP_FILES
                if not (art / "app" / n).is_file()]
    if missing:
        raise FileNotFoundError(
            f"artifact snapshot incomplete; missing: {', '.join(missing)}")

    repo_dir = _GIT_FIXTURE_ROOT / incident_id
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True)

    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.email", "night-shift@localhost")
    _git(repo_dir, "config", "user.name", "Night Shift")

    # Original requirements: exact bytes from the artifact.
    shutil.copyfile(art / "requirements-original.txt", repo_dir / "requirements.txt")
    # App snapshot: QA-tested files, copied from <art>/app.
    for name in _SNAPSHOT_APP_FILES:
        shutil.copyfile(art / "app" / name, repo_dir / name)

    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "base: unpatched application snapshot")
    return repo_dir


def create_proposal(incident_id: str, version: str, artifact_dir: str,
                    artifact_sha256: str, manifest: dict) -> Dict[str, Any]:
    """Create a Git proposal branch committing the artifact's exact
    requirements-proposed bytes.

    Returns a dict with base_commit, proposal_commit, tree, diff — or
    {"error": ...} on any failure. Missing artifact files fail closed.
    """
    try:
        repo_dir = _init_fixture_repo(incident_id, artifact_dir)
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Fixture repo init failed: {e}"}

    branch = f"remediate-{incident_id[:8]}"

    r = _git(repo_dir, "checkout", "-b", branch)
    if r.returncode != 0:
        return {"error": f"Branch creation failed: {r.stderr.strip()}"}

    # The artifact's EXACT proposed bytes — no reconstruction.
    proposed_bytes = (Path(artifact_dir) / "requirements-proposed.txt").read_text()
    (repo_dir / "requirements.txt").write_text(proposed_bytes)

    _git(repo_dir, "add", "requirements.txt")
    r = _git(repo_dir, "commit", "-m",
             f"propose: {REMEDIATION_PACKAGE}=={version} (artifact {artifact_sha256[:16]})")
    if r.returncode != 0:
        return {"error": f"Commit failed: {r.stderr.strip()}"}

    base_r = _git(repo_dir, "rev-parse", "HEAD~1")
    prop_r = _git(repo_dir, "rev-parse", "HEAD")
    tree_r = _git(repo_dir, "rev-parse", "HEAD^{tree}")
    diff_r = _git(repo_dir, "diff", "HEAD~1", "HEAD")
    ls_r = _git(repo_dir, "ls-files")

    if any(x.returncode != 0 for x in [base_r, prop_r, tree_r, diff_r, ls_r]):
        return {"error": "Git identifier capture failed"}

    tracked = set(ls_r.stdout.split())
    absent = [n for n in _REQUIRED_COMMITTED_FILES if n not in tracked]
    if absent:
        return {"error": f"committed tree missing QA-tested files: {', '.join(absent)}"}

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
    """Genuine isolated verification of the committed checkout.

    Builds a disposable venv, installs the committed requirements bytes,
    measures installed versions from that environment, and runs the
    committed regression tests with the strict evidence rules of
    verify_patch: pytest must collect tests, none may be skipped/failed/
    errored, all required tests must pass, and the JUnit evidence must be
    structurally valid. Missing tests, zero collected, installation
    failure, or invalid evidence all reject.
    """
    repo_dir = Path(proposal["repo_dir"])
    result: Dict[str, Any] = {
        "proposal_commit": proposal.get("proposal_commit"),
        "proposal_tree": proposal.get("proposal_tree"),
        "branch": proposal.get("branch"),
        "verified": False,
        "error": None,
        "installed_version": None,
        "installed_version_source": None,
        "installed_dependency_versions": {},
        "pytest_returncode": None,
        "collected_tests": None,
        "junit": None,
        "required_tests_ok": False,
        "success": False,
        "test_output": "",
    }

    def fail(msg: str) -> Dict[str, Any]:
        result["error"] = msg
        return result

    # 0. The checkout must be exactly the proposal commit, clean.
    r = _git(repo_dir, "rev-parse", "HEAD")
    if r.returncode != 0 or r.stdout.strip() != proposal["proposal_commit"]:
        return fail("Not on the expected proposal commit")
    r = _git(repo_dir, "status", "--porcelain")
    if r.returncode != 0:
        return fail("Git status failed")
    if r.stdout.strip():
        return fail(f"Dirty working tree: {r.stdout.strip()[:200]}")

    # 1. Committed files must all be present.
    absent = [n for n in _REQUIRED_COMMITTED_FILES
              if not (repo_dir / n).is_file()]
    if absent:
        return fail(f"committed checkout missing files: {', '.join(absent)}")

    # 2. Validate the committed requirements bytes against the strict
    # installer policy BEFORE any package-install subprocess runs.
    req_path = repo_dir / "requirements.txt"
    req_bytes = req_path.read_text()
    req_reason = validate_requirements(req_bytes, proposal["proposed_version"])
    if req_reason:
        return fail(f"committed requirements rejected by installer policy: {req_reason}")

    # 3. Disposable venv built from the committed requirements bytes.
    workdir = Path(tempfile.mkdtemp(prefix="nightshift-gitverify-"))
    venv_dir = workdir / ".venv"
    try:
        rc, out, err = _run(["python3", "-m", "venv", str(venv_dir)], cwd=workdir)
        if rc != 0:
            return fail(f"venv creation failed: {err}")
        pip = venv_dir / "bin" / "pip"
        python = venv_dir / "bin" / "python"

        rc, out, err = _run([str(pip), "install", "-q", "-r", str(req_path)],
                            cwd=workdir)
        if rc != 0:
            result["test_output"] = (out + err)[-500:]
            return fail(f"install of committed requirements failed: {err}")

        rc, out, err = _run([str(pip), "install", "-q", VERIFICATION_TOOL_PIN],
                            cwd=workdir)
        if rc != 0:
            return fail(f"pytest install failed: {err}")

        # 4. Measure installed versions FROM the built environment and
        # require them to match the approved exact pins.
        rc, out, err = _run(
            [str(python), "-c",
             "from importlib.metadata import version; print(version('%s'))"
             % REMEDIATION_PACKAGE],
            cwd=workdir,
        )
        if rc != 0 or not out.strip():
            return fail(f"version inspection failed: {err or 'empty output'}")
        measured = out.strip()
        result["installed_version"] = measured
        result["installed_version_source"] = "measured"
        if measured != proposal["proposed_version"]:
            return fail(
                f"measured {REMEDIATION_PACKAGE} version {measured} does not "
                f"match the approved pin {proposal['proposed_version']}")

        dep_versions = {}
        for dep in ("MarkupSafe", "pytest"):
            drc, dout, _ = _run(
                [str(python), "-c",
                 "from importlib.metadata import version; print(version('%s'))" % dep],
                cwd=workdir,
            )
            if drc == 0 and dout.strip():
                dep_versions[dep] = dout.strip()
        result["installed_dependency_versions"] = dep_versions

        # Supporting pins must measure exactly their committed pin values.
        for pin in SUPPORTING_PINS:
            name, _, expected = pin.partition("==")
            measured_dep = dep_versions.get(name)
            if measured_dep is None:
                return fail(f"supporting dependency {name} not measurable")
            if measured_dep != expected:
                return fail(
                    f"measured {name} version {measured_dep} does not match "
                    f"the exact pin {expected}")

        # 4. Run the committed tests against the committed app code. JUnit
        # output goes to the temp workdir (absolute path) so the fixture
        # repo's working tree stays clean.
        junit_path = workdir / "junit.xml"
        rc, test_out, test_err = _run(
            [str(python), "-m", "pytest", "-q",
             f"--junitxml={junit_path}"],
            cwd=repo_dir,
        )
        result["pytest_returncode"] = rc
        result["test_output"] = (test_out + "\n" + test_err).strip()[-500:]

        junit = _parse_junit(junit_path) if junit_path.exists() else {
            "parse_error": "junit.xml not produced"}
        result["junit"] = junit
        statuses = junit.get("tests", {})
        total = len(statuses)
        result["collected_tests"] = junit.get("total", total)

        if junit.get("parse_error"):
            return fail(f"invalid test evidence: {junit['parse_error']}")
        if total == 0:
            return fail("zero tests collected from the committed checkout")
        if junit.get("failures") or junit.get("errors") or junit.get("skipped"):
            return fail(
                f"committed tests not clean: "
                f"failures={junit.get('failures')} errors={junit.get('errors')} "
                f"skipped={junit.get('skipped')}")
        result["required_tests_ok"] = all(
            statuses.get(t) == "passed" for t in REQUIRED_TESTS)
        if not result["required_tests_ok"]:
            missing = [t for t in REQUIRED_TESTS if statuses.get(t) != "passed"]
            return fail(f"required tests not all passed: {missing}")

        if rc != 0:
            return fail(f"pytest return code {rc}")

        result["verified"] = True
        result["success"] = True
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def validate_proposal_pre_approval(proposal: dict) -> Optional[str]:
    """Pre-approval validation of a Git proposal: must be a clean, verified
    checkout with no stored error. This runs BEFORE approval exists, so it
    compares the checkout against the proposal's own recorded identifiers —
    the post-approval binding (check_approval_binding) separately rechecks
    against the immutable approval record.
    """
    if not isinstance(proposal, dict) or not proposal.get("repo_dir"):
        return "No Git proposal stored"
    if proposal.get("error"):
        return f"Git proposal carries a stored error: {proposal.get('error')}"
    if not proposal.get("verified"):
        return ("Git proposal verification missing or failed: "
                f"{proposal.get('error') or 'verification did not pass'}")
    repo_dir = Path(proposal["repo_dir"])
    if not repo_dir.exists():
        return "Fixture repository directory missing"
    r = _git(repo_dir, "rev-parse", "HEAD")
    if r.returncode != 0:
        return "Cannot read current HEAD"
    if r.stdout.strip() != proposal.get("proposal_commit"):
        return "Checkout is not on the proposal commit"
    r = _git(repo_dir, "rev-parse", "HEAD^{tree}")
    if r.returncode != 0 or r.stdout.strip() != proposal.get("proposal_tree"):
        return "Checkout tree does not match the proposal tree"
    r = _git(repo_dir, "status", "--porcelain")
    if r.returncode != 0:
        return "Git status failed"
    if r.stdout.strip():
        return f"Dirty working tree: {r.stdout.strip()[:200]}"
    return None


def check_approval_binding(incident: dict, proposal: dict) -> Optional[str]:
    """Recheck the Git state against the APPROVAL RECORD's commit/tree.

    The approval record is the only trusted source of the approved
    commit/tree; mutable proposal identifiers are never used as a
    fallback. A missing binding fails closed. Returns None if clean, or
    an error string identifying the broken binding.
    """
    approval = incident.get("human_approval") or {}
    approval_git = approval.get("git") or {}
    expected_commit = approval_git.get("commit")
    expected_tree = approval_git.get("tree")
    if not expected_commit or not expected_tree:
        return "No approved Git commit/tree binding on record; refusing to trust mutable identifiers"

    repo_dir = Path(proposal["repo_dir"])
    if not repo_dir.exists():
        return "Fixture repository directory missing"

    r = _git(repo_dir, "rev-parse", "HEAD")
    if r.returncode != 0:
        return "Cannot read current HEAD"
    current_commit = r.stdout.strip()
    if current_commit != expected_commit:
        return (f"Git content changed after approval: approved commit "
                f"{expected_commit[:12]}, checkout is at {current_commit[:12]}")

    r = _git(repo_dir, "rev-parse", "HEAD^{tree}")
    if r.returncode != 0 or r.stdout.strip() != expected_tree:
        return ("Git tree changed after approval: approved tree "
                f"{str(expected_tree)[:12]} does not match checkout")

    r = _git(repo_dir, "status", "--porcelain")
    if r.returncode != 0:
        return "Git status failed"
    if r.stdout.strip():
        return f"Dirty working tree: {r.stdout.strip()[:200]}"

    return None


def mutate_requirements_for_test(proposal: dict) -> str:
    """Trusted test helper: deliberately alter the approved requirements file
    to simulate a post-approval mutation. Returns the content written.
    """
    repo_dir = Path(proposal["repo_dir"])
    malicious = f"{REMEDIATION_PACKAGE}==9.9.9\n# MUTATED BY INTEGRITY TEST\n"
    (repo_dir / "requirements.txt").write_text(malicious)
    return malicious
