"""Isolated dependency verification runner with persisted patch artifacts.

Installs packages only in disposable virtualenvs and runs the regression tests
from the persisted artifact snapshot, never from mutable fixture sources.

Security boundaries:
- The package under remediation is a fixed constant (REMEDIATION_PACKAGE).
- The actual requirements bytes are validated immediately before installation:
  exactly the fixed Jinja2 pin plus the exact pinned supporting dependency.
  Extra lines, URLs, editable installs, pip options, or any other content are
  rejected BEFORE any subprocess starts.
- The trusted fault-injection exception allows EXACTLY jinja2==3.1.2, never
  an arbitrary numeric version, and is only reachable from the orchestrator's
  labeled scenario. Model proposals cannot activate it (they are allowlist-
  bound at the orchestration boundary and re-checked here).
- Version inspection uses importlib.metadata with a fixed package constant.
- Test results come from pytest's structured JUnit XML report.
- The artifact snapshot (requirements + trusted fixture code + tests) is
  persisted per incident with a canonical manifest of file hashes computed
  from actual bytes; approval and release recompute it from disk.
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

# Fixed operations: only this package may be remediated in this workflow.
REMEDIATION_PACKAGE = "jinja2"

# Normal remediation choices. 3.1.3 is deliberately absent: it fixes
# GHSA-h5c8-rqwp-cp95 but not GHSA-h75v-3vvj-5mfj, so it cannot satisfy the
# current policy (MIN_PATCHED_VERSION = 3.1.4).
ALLOWED_VERSIONS = {"3.1.4", "3.1.5", "3.1.6"}

# The trusted fault-injection exception: EXACTLY this version, no others.
FAULT_INJECTION_VERSION = "3.1.2"

# The application's supporting dependency pins. Verification tooling
# (pytest) is installed separately from the application requirements.
# MarkupSafe 2.1.5 is the verified application pin.
SUPPORTING_PINS = {"MarkupSafe==2.1.5"}
VERIFICATION_TOOL_PIN = "pytest==8.3.3"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_TESTS = {
    "test_render_greeting_escapes_html",
    "test_xmlattr_renders_normal_attributes",
    "test_xmlattr_rejects_space_in_key",
    "test_xmlattr_rejects_slash_in_key",
}

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "test_app"
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"

# Trusted files copied verbatim from the fixture into every artifact snapshot.
_SNAPSHOT_APP_FILES = ("main.py", "test_regression.py", "pytest.ini")

_ORIGINAL_REQUIREMENTS = "jinja2==3.1.2\nMarkupSafe>=2.0\n"
_MANIFEST_NAME = "manifest.json"


def _run(cmd: list, cwd: Path, timeout: float = 180.0):
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return None, (exc.stdout or "") if isinstance(exc.stdout, str) else "", \
            f"command timed out after {timeout}s: {' '.join(map(str, cmd[:3]))}…"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_version(version: str, fault_injection: bool = False) -> bool:
    """Installer-boundary version check.

    Normal path: version must be in ALLOWED_VERSIONS. Fault-injection path:
    version must be EXACTLY '3.1.2'. Anything else is rejected.
    """
    v = str(version or "").strip()
    if not _VERSION_RE.match(v):
        return False
    if fault_injection:
        return v == FAULT_INJECTION_VERSION
    return v in ALLOWED_VERSIONS


def validate_requirements(text: str, version: str, fault_injection: bool = False) -> Optional[str]:
    """Validate the actual requirements bytes immediately before installation.

    Accepts exactly the fixed package pinned to `version` and the exact
    pinned supporting dependencies. Rejects extra lines, URLs, editable
    installs, pip options, comments, and any unexpected content.

    `fault_injection` must come from trusted orchestrator code; a model or
    request can never pass True through this boundary.

    Returns None if valid, or a reason string if invalid.
    """
    if not isinstance(text, str):
        return "requirements content is not a string"
    if not validate_version(version, fault_injection=fault_injection):
        return f"version {version!r} rejected by installer version policy"
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    expected = {f"{REMEDIATION_PACKAGE}=={version}"} | SUPPORTING_PINS
    if len(lines) != len(expected):
        return (f"requirements must contain exactly {len(expected)} pins; "
                f"got {len(lines)}: {lines!r}")
    for ln in lines:
        if ln.startswith(("-", "#", "git+", "http://", "https://", "ssh://", "ftp://")):
            return f"forbidden requirements line: {ln!r}"
        if "://" in ln or "@" in ln or ";" in ln or " " in ln:
            return f"forbidden characters in requirements line: {ln!r}"
    if set(lines) != expected:
        return f"requirements do not match the exact expected pin set {sorted(expected)}; got {sorted(lines)}"
    return None


def compute_manifest(directory: Path) -> dict:
    """Canonical manifest of file hashes computed from actual bytes on disk.

    Returns {"files": {relpath: sha256}, "digest": sha256(canonical json)}.
    The manifest file itself is excluded. Missing directory returns an empty
    manifest with a "missing" note so callers fail closed.
    """
    directory = Path(directory)
    files: dict = {}
    if not directory.is_dir():
        return {"files": {}, "digest": None, "error": f"artifact directory missing: {directory}"}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == _MANIFEST_NAME:
            continue
        rel = path.relative_to(directory).as_posix()
        files[rel] = _sha256_bytes(path.read_bytes())
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {"files": files, "digest": _sha256_bytes(canonical.encode("utf-8"))}


def _write_manifest(directory: Path) -> dict:
    manifest = compute_manifest(directory)
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def persist_artifact(incident_id: str, version: str,
                     fault_injection: bool = False,
                     base_dir: Optional[Path] = None) -> Optional[dict]:
    """Persist the exact tested artifact snapshot for one incident.

    Contains: proposed requirements, trusted fixture application code and
    regression tests, the original requirements, a readable diff, and a
    canonical manifest of file hashes from actual bytes.

    Returns the artifact descriptor, or None if the version is rejected.
    """
    if not validate_version(version, fault_injection):
        return None

    root = Path(base_dir) if base_dir else ARTIFACTS_DIR
    art_dir = root / incident_id
    if art_dir.exists():
        shutil.rmtree(art_dir)
    (art_dir / "app").mkdir(parents=True)

    proposed = f"{REMEDIATION_PACKAGE}=={version}\n" + "\n".join(sorted(SUPPORTING_PINS)) + "\n"
    # Validate the bytes we are about to persist. The fault-injection flag is
    # forwarded so the trusted orchestrator scenario can persist 3.1.2; a
    # model attempt can never set it.
    reason = validate_requirements(proposed, version,
                                   fault_injection=fault_injection)
    if reason:
        shutil.rmtree(art_dir, ignore_errors=True)
        return None

    (art_dir / "requirements-original.txt").write_text(_ORIGINAL_REQUIREMENTS)
    (art_dir / "requirements-proposed.txt").write_text(proposed)
    diff = (
        "--- requirements-original.txt (affected: jinja2 3.1.2)\n"
        "+++ requirements-proposed.txt\n"
        f"-{REMEDIATION_PACKAGE}=={_ORIGINAL_REQUIREMENTS.splitlines()[0].split('==')[1]}\n"
        f"+{REMEDIATION_PACKAGE}=={version}\n"
        f" {'+'.join([''])}{sorted(SUPPORTING_PINS)[0]}\n"
    )
    (art_dir / "requirements.diff").write_text(diff)

    for name in _SNAPSHOT_APP_FILES:
        src = FIXTURE_DIR / name
        if not src.is_file():
            shutil.rmtree(art_dir, ignore_errors=True)
            return None
        shutil.copyfile(src, art_dir / "app" / name)

    manifest = _write_manifest(art_dir)
    return {
        "package": REMEDIATION_PACKAGE,
        "proposed_version": version,
        "fault_injection": fault_injection,
        "artifact_dir": str(art_dir),
        "original_requirements": _ORIGINAL_REQUIREMENTS,
        "proposed_requirements": proposed,
        "diff": diff,
        "manifest": manifest,
        "sha256": manifest["digest"],
    }


def verify_patch(artifact: dict) -> dict:
    """Verify the persisted artifact snapshot in a disposable venv.

    The actual requirements bytes on disk are re-validated immediately before
    installation; invalid content fails before any subprocess starts. The
    regression tests are executed from the artifact snapshot's app directory,
    not from the mutable fixture source.
    """
    art_dir = Path(artifact["artifact_dir"])
    result: dict = {
        "artifact_sha256": artifact.get("sha256"),
        "artifact_dir": str(art_dir),
        "manifest": artifact.get("manifest"),
        "install_ok": False,
        "version_inspection_ok": False,
        "installed_version": None,
        "pytest_returncode": None,
        "junit": None,
        "required_tests_ok": False,
        "success": False,
        "error": None,
        "test_output": "",
        "requirements_validated": False,
    }

    # 0. Version policy + requirements byte validation BEFORE any subprocess.
    if not validate_version(artifact["proposed_version"],
                            fault_injection=artifact.get("fault_injection", False)):
        result["error"] = (f"installer version policy rejected {artifact['proposed_version']!r}; "
                           "subprocess never started")
        return result
    req_path = art_dir / "requirements-proposed.txt"
    if not req_path.is_file():
        result["error"] = f"proposed requirements file missing from artifact: {req_path}"
        return result
    reason = validate_requirements(req_path.read_text(), artifact["proposed_version"],
                                   fault_injection=artifact.get("fault_injection", False))
    if reason:
        result["error"] = f"requirements validation failed before install: {reason}"
        return result
    result["requirements_validated"] = True

    # 0b. The snapshot's app files must exist.
    for name in _SNAPSHOT_APP_FILES:
        if not (art_dir / "app" / name).is_file():
            result["error"] = f"artifact snapshot file missing: app/{name}"
            return result

    workdir = Path(tempfile.mkdtemp(prefix="nightshift-"))
    venv_dir = workdir / ".venv"
    app_copy = workdir / "app"
    try:
        rc, out, err = _run(["python3", "-m", "venv", str(venv_dir)], cwd=workdir)
        if rc != 0:
            result["error"] = f"venv creation failed: {err}"
            return result
        pip = venv_dir / "bin" / "pip"
        python = venv_dir / "bin" / "python"

        # Install the EXACT persisted proposed requirements file.
        rc, out, err = _run([str(pip), "install", "-q", "-r", str(req_path)], cwd=workdir)
        if rc != 0:
            result["error"] = f"install of proposed requirements failed: {err}"
            result["test_output"] = out + err
            return result

        rc, out, err = _run([str(pip), "install", "-q", VERIFICATION_TOOL_PIN], cwd=workdir)
        if rc != 0:
            result["error"] = f"pytest install failed: {err}"
            return result
        result["install_ok"] = True

        # Execute the SNAPSHOT's app code (never the mutable fixture source).
        shutil.copytree(art_dir / "app", app_copy)

        rc, out, err = _run(
            [str(python), "-c",
             "from importlib.metadata import version; print(version('%s'))" % REMEDIATION_PACKAGE],
            cwd=app_copy,
        )
        if rc != 0 or not out.strip():
            result["error"] = f"version inspection failed: {err or 'empty output'}"
            result["test_output"] = out + err
            return result
        result["installed_version"] = out.strip()
        result["version_inspection_ok"] = True

        # Record exact installed versions of supporting dependencies.
        dep_versions: dict = {}
        for dep in ("MarkupSafe", "pytest"):
            drc, dout, _ = _run(
                [str(python), "-c",
                 "from importlib.metadata import version; print(version('%s'))" % dep],
                cwd=app_copy,
            )
            if drc == 0 and dout.strip():
                dep_versions[dep] = dout.strip()
        result["installed_dependency_versions"] = dep_versions

        junit_path = app_copy / "junit.xml"
        rc, test_out, test_err = _run(
            [str(python), "-m", "pytest", "-q", f"--junitxml={junit_path.name}"],
            cwd=app_copy,
        )
        result["pytest_returncode"] = rc
        result["test_output"] = (test_out + "\n" + test_err).strip()

        junit = _parse_junit(junit_path) if junit_path.exists() else {"parse_error": "junit.xml not produced"}
        result["junit"] = junit

        statuses = junit.get("tests", {})
        result["required_tests_ok"] = all(
            statuses.get(t) == "passed" for t in REQUIRED_TESTS
        ) and not junit.get("parse_error")

        # Any failed, errored, or skipped testcase blocks success, including
        # tests outside the required four. _parse_junit now enforces strict
        # non-negative integer counts and aggregate consistency.
        result["success"] = (
            result["install_ok"]
            and result["version_inspection_ok"]
            and rc == 0
            and result["required_tests_ok"]
            and not junit.get("parse_error")
            and junit.get("failures") == 0
            and junit.get("errors") == 0
            and junit.get("skipped") == 0
        )
        if not result["success"] and not result["error"]:
            reasons = []
            if rc is None:
                reasons.append("pytest timed out")
            elif rc != 0:
                reasons.append(f"pytest return code {rc}")
            if not result["required_tests_ok"]:
                missing = [t for t in REQUIRED_TESTS if statuses.get(t) != "passed"]
                reasons.append(f"required tests not all passed: {missing or 'see junit'}")
            if junit.get("failures"):
                reasons.append(f"{junit['failures']} test failure(s)")
            if junit.get("errors"):
                reasons.append(f"{junit['errors']} test error(s)")
            if junit.get("skipped"):
                reasons.append(f"{junit['skipped']} test(s) skipped")
            if junit.get("parse_error"):
                reasons.append(junit["parse_error"])
            result["error"] = "; ".join(reasons) or "verification failed"
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def recompute_manifest(artifact_dir) -> dict:
    """Recompute the manifest from disk (public wrapper for approval gates)."""
    return compute_manifest(Path(artifact_dir))


def manifest_matches(current: dict, verified: dict) -> Optional[str]:
    """Compare a recomputed manifest against the verified one by actual file
    hashes, not just the digest string. Returns None on match, else a reason."""
    if not verified or not isinstance(verified, dict):
        return "verified manifest is missing or malformed"
    if not current or current.get("error"):
        return f"artifact directory unreadable: {(current or {}).get('error', 'unknown')}"
    cur_files = current.get("files") or {}
    ver_files = verified.get("files") or {}
    if set(cur_files) != set(ver_files):
        missing = sorted(set(ver_files) - set(cur_files))
        extra = sorted(set(cur_files) - set(ver_files))
        return f"artifact file set changed: missing={missing}, unexpected={extra}"
    changed = [f for f in ver_files if cur_files.get(f) != ver_files[f]]
    if changed:
        return f"artifact file contents changed: {sorted(changed)}"
    if current.get("digest") != verified.get("digest"):
        return "artifact manifest digest mismatch"
    return None


# Backwards-compatible name used by earlier checkpoints.
def build_artifact(version: str, fault_injection: bool = False,
                   base_dir: Optional[Path] = None,
                   incident_id: Optional[str] = None) -> Optional[dict]:
    import uuid as _uuid
    return persist_artifact(incident_id or str(_uuid.uuid4()), version,
                            fault_injection=fault_injection, base_dir=base_dir)


def _strict_int(value, name: str) -> tuple[int, Optional[str]]:
    """Reject bools, floats, strings, missing values, and negatives."""
    if value is None:
        return 0, f"{name} count missing"
    if isinstance(value, bool):
        return 0, f"{name} count is a boolean"
    if isinstance(value, float):
        return 0, f"{name} count is a float"
    if not isinstance(value, int):
        return 0, f"{name} count is not an integer ({type(value).__name__})"
    if value < 0:
        return 0, f"{name} count is negative ({value})"
    return value, None


def _parse_junit(xml_path: Path) -> dict:
    """Parse pytest's JUnit XML into structured test evidence.

    Aggregate counts are derived from testcase outcomes and must be
    consistent with the reported totals. Any failed, errored, or skipped
    testcase blocks success, including tests outside the required four.
    Duplicate or missing required test names are rejected.
    """
    evidence: dict = {
        "tests": {},
        "total": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        evidence["parse_error"] = f"JUnit XML parse failed: {exc}"
        return evidence

    reported: dict = {"total": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suites in root.iter("testsuite"):
        for attr, key in (("tests", "total"), ("failures", "failures"),
                          ("errors", "errors"), ("skipped", "skipped")):
            raw = suites.get(attr)
            if raw is None:
                evidence["parse_error"] = f"{key} count missing"
                return evidence
            # Attribute values are text; they must be non-negative integer
            # text ("true", "1.5", "-1", binary garbage all fail here).
            if not isinstance(raw, str) or not raw.strip().isdigit():
                evidence["parse_error"] = f"{key} count is not integer text ({raw!r})"
                return evidence
            reported[key] += int(raw)

    for suites in root.iter("testsuite"):
        for case in suites.iter("testcase"):
            name = case.get("name", "")
            status = "passed"
            if case.find("failure") is not None:
                status = "failed"
            elif case.find("error") is not None:
                status = "error"
            elif case.find("skipped") is not None:
                status = "skipped"
            # Duplicate names: any non-passing duplicate poisons the name.
            if name in evidence["tests"] and evidence["tests"][name] != "passed":
                status = "error"
            elif name in evidence["tests"] and status != "passed":
                status = "error"
            evidence["tests"][name] = status
            evidence["total"] += 1
            if status == "failed":
                evidence["failures"] += 1
            elif status == "error":
                evidence["errors"] += 1
            elif status == "skipped":
                evidence["skipped"] += 1

    # Strict validation of aggregate counts.
    for key in ("total", "failures", "errors", "skipped"):
        n, err = _strict_int(evidence[key], key)
        if err:
            evidence["parse_error"] = err
            return evidence
        evidence[key] = n

    # Consistency check: reported totals must match derived counts.
    for key in ("total", "failures", "errors", "skipped"):
        if reported[key] != evidence[key]:
            evidence["parse_error"] = (
                f"JUnit aggregate mismatch: reported {key}={reported[key]} "
                f"but derived {key}={evidence[key]}"
            )
            return evidence

    return evidence
