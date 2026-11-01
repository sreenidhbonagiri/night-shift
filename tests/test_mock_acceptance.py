"""Mock-based integrity tests for Night Shift (Integrity Checkpoint).

No model calls and no package installs occur in this file. All agent and
runner functions are injected mocks; artifact snapshots are persisted into
temporary directories by the real persist_artifact code path.
These tests exercise real behavior of the installer boundary, evidence gates,
artifact persistence/mutation detection, approval gates, and rejection
feedback.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import workflow as wf
from app import approval as ap
from app import evidence as evidence_mod
from app import isolated_runner as ir
from app.qa_agent import verify as qa_verify
from app.isolated_runner import (
    REQUIRED_TESTS, persist_artifact, verify_patch, validate_requirements,
    validate_version, compute_manifest, manifest_matches, recompute_manifest,
    _parse_junit,
)

REQUIRED_LIST = sorted(REQUIRED_TESTS)


# ---------------------------------------------------------------- helpers

def make_verification(installed="3.1.4", proposed="3.1.4", rc=0,
                      tests=None, failures=0, errors=0, skipped=0,
                      install_ok=True, inspection_ok=True, sha="a" * 64,
                      artifact=None):
    """Build a realistic structured verification dict (mock runner output)."""
    if tests is None:
        tests = {t: "passed" for t in REQUIRED_TESTS}
    return {
        "install_ok": install_ok,
        "version_inspection_ok": inspection_ok,
        "installed_version": installed,
        "proposed_version": proposed,
        "pytest_returncode": rc,
        "junit": {"tests": dict(tests), "total": len(tests), "failures": failures,
                  "errors": errors, "skipped": skipped},
        "required_tests_ok": all(tests.get(t) == "passed" for t in REQUIRED_TESTS),
        "success": rc == 0 and failures == 0 and errors == 0 and skipped == 0,
        "error": None,
        "test_output": "3 passed in 0.01s",
        "artifact_sha256": sha,
        "artifact_dir": artifact["artifact_dir"] if artifact else None,
        "manifest": artifact["manifest"] if artifact else None,
        "artifact": artifact,
        "installer_invoked": True,
        "requirements_validated": True,
        "deterministic_checks": None,
    }


def good_model_fn(system_prompt, user_prompt):
    return {"success": True, "response": {"approval": True, "reason": "Evidence is consistent."},
            "raw": '{"approval": true, "reason": "Evidence is consistent."}', "elapsed_ms": 1.0, "error": None}


def make_sentry(disposition):
    def sentry_fn(log_key):
        return {"log_key": log_key, "log": "synthetic", "disposition": disposition,
                "severity": "INFO", "operational_priority": "LOW", "reason": "mock",
                "advisory_severity": "MODERATE", "model": "mock",
                "elapsed_ms": 1.0, "utc_timestamp": "2026-10-31T00:00:00+00:00",
                "error": None, "raw": "{}"}
    return sentry_fn


def make_engineer(versions):
    """Engineer mock returning successive proposals."""
    it = iter(versions)

    def engineer_fn(ctx, prev=None):
        v = next(it)
        return {"proposal": {"package": "jinja2", "version": v, "reason": f"upgrade to {v}"},
                "reason": f"upgrade to {v}", "model": "mock", "elapsed_ms": 1.0,
                "utc_timestamp": "t", "error": None, "raw": "{}"}
    return engineer_fn


class ArtifactTmpBase(unittest.TestCase):
    """Base: run incidents with artifacts persisted in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ns-mock-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def run_incident(self, **kw):
        kw.setdefault("artifact_dir_root", Path(self._tmp))
        kw.setdefault("sentry_fn", make_sentry("ACTIONABLE"))
        return wf.run_incident(**kw)


# --------------------------------------------- 1. Installer boundary tests

class TestInstallerBoundary(unittest.TestCase):

    def test_validate_requirements_accepts_exact_pins(self):
        self.assertIsNone(validate_requirements(
            "MarkupSafe==2.1.5\njinja2==3.1.4\n", "3.1.4"))

    def test_validate_requirements_rejects_extra_line(self):
        reason = validate_requirements("jinja2==3.1.4\nMarkupSafe==2.1.5\nrequests==2.0.0\n", "3.1.4")
        self.assertIsNotNone(reason)
        self.assertIn("pins", reason)

    def test_validate_requirements_rejects_url(self):
        reason = validate_requirements("jinja2==3.1.4\nhttps://evil.example/x.whl\n", "3.1.4")
        self.assertIsNotNone(reason)

    def test_validate_requirements_rejects_editable(self):
        reason = validate_requirements("jinja2==3.1.4\nMarkupSafe==2.1.5\n-e git+https://evil.example/repo\n", "3.1.4")
        self.assertIsNotNone(reason)

    def test_validate_requirements_rejects_pip_option(self):
        reason = validate_requirements("jinja2==3.1.4\nMarkupSafe==2.1.5\n--index-url http://evil\n", "3.1.4")
        self.assertIsNotNone(reason)

    def test_validate_requirements_rejects_wrong_version_pin(self):
        reason = validate_requirements("jinja2==3.1.2\nMarkupSafe==2.1.5\n", "3.1.4")
        self.assertIsNotNone(reason)

    def test_verify_patch_rejects_modified_requirements_before_subprocess(self):
        """Mutate the persisted requirements file; verify_patch must fail
        BEFORE creating any venv (no subprocess), for the requirements reason."""
        with tempfile.TemporaryDirectory() as tmp:
            art = persist_artifact("inc-req-mut", "3.1.4", base_dir=Path(tmp))
            req = Path(art["artifact_dir"]) / "requirements-proposed.txt"
            req.write_text("jinja2==3.1.4\nMarkupSafe==2.1.5\nevil-package==1.0\n")
            result = verify_patch(art)
            self.assertFalse(result["success"])
            self.assertFalse(result["requirements_validated"])
            self.assertIn("requirements validation failed", result["error"])
            self.assertFalse(result["install_ok"])
            self.assertIsNone(result["pytest_returncode"])  # pytest never ran

    def test_verify_patch_missing_requirements_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = persist_artifact("inc-req-miss", "3.1.4", base_dir=Path(tmp))
            (Path(art["artifact_dir"]) / "requirements-proposed.txt").unlink()
            result = verify_patch(art)
            self.assertFalse(result["success"])
            self.assertIn("missing from artifact", result["error"])

    def test_fault_injection_exception_is_exactly_3_1_2(self):
        self.assertTrue(validate_version("3.1.2", fault_injection=True))
        for v in ("3.1.3", "3.1.4", "9.9.9", "3.1.2; rm -rf /", ""):
            self.assertFalse(validate_version(v, fault_injection=True), v)

    def test_fault_injection_cannot_be_activated_by_model(self):
        """A model proposal of 3.1.2 (no orchestrator fault flag) is rejected;
        and a model attempt that forges fault_injection=True still only gets
        allowlist treatment for a non-allowed version."""
        # Direct boundary check:
        self.assertFalse(validate_version("3.1.2", fault_injection=False))
        # Workflow-level: model proposes 3.1.2, never reaches installer.
        with tempfile.TemporaryDirectory() as tmp:
            runner = MagicMock()
            inc = wf.run_incident(engineer_fn=make_engineer(["3.1.2", "3.1.2"]),
                                  sentry_fn=make_sentry("ACTIONABLE"),
                                  runner_fn=runner,
                                  qa_fn=lambda v: {"approval": False, "reason": "mock"},
                                  fault_inject_first=False,
                                  artifact_dir_root=Path(tmp))
            runner.assert_not_called()
            self.assertEqual(inc["status"], "remediation_failed")
            self.assertIn("boundary", inc["attempts"][0]["verification"]["error"])

        # Forged fault_injection flag on a MODEL attempt: the workflow keys
        # the exception on the orchestrator scenario only (attempt 1 +
        # fault_inject_first), so a forged flag cannot widen the allowlist.
        forged = lambda ctx, prev=None: {
            "proposal": {"package": "jinja2", "version": "3.1.2", "reason": "forged"},
            "reason": "forged", "model": "mock", "elapsed_ms": 1.0,
            "utc_timestamp": "t", "error": None,
            "fault_injection": True, "raw": "{}"}
        with tempfile.TemporaryDirectory() as tmp:
            runner = MagicMock()
            inc = wf.run_incident(engineer_fn=forged,
                                  sentry_fn=make_sentry("ACTIONABLE"),
                                  runner_fn=runner,
                                  qa_fn=lambda v: {"approval": False, "reason": "mock"},
                                  fault_inject_first=False,
                                  artifact_dir_root=Path(tmp))
            runner.assert_not_called()
            self.assertEqual(inc["status"], "remediation_failed")

    def test_normal_versions_boundary(self):
        self.assertTrue(validate_version("3.1.4"))
        self.assertTrue(validate_version("3.1.5"))
        self.assertTrue(validate_version("3.1.6"))
        self.assertFalse(validate_version("3.1.3"))  # cannot satisfy policy
        self.assertFalse(validate_version("9.9.9"))
        self.assertFalse(validate_version("3.1.4; rm -rf /"))

    def test_model_non_allowlisted_version_blocked_at_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = MagicMock()
            inc = wf.run_incident(engineer_fn=make_engineer(["9.9.9", "9.9.9"]),
                                  sentry_fn=make_sentry("ACTIONABLE"),
                                  runner_fn=runner,
                                  qa_fn=lambda v: {"approval": False, "reason": "mock"},
                                  fault_inject_first=False,
                                  artifact_dir_root=Path(tmp))
            runner.assert_not_called()
            self.assertEqual(inc["status"], "remediation_failed")
            self.assertIn("boundary", inc["attempts"][0]["verification"]["error"])

    def test_model_3_1_3_blocked_at_boundary(self):
        """3.1.3 is no longer a valid remediation choice."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = MagicMock()
            inc = wf.run_incident(engineer_fn=make_engineer(["3.1.3", "3.1.3"]),
                                  sentry_fn=make_sentry("ACTIONABLE"),
                                  runner_fn=runner,
                                  qa_fn=lambda v: {"approval": False, "reason": "mock"},
                                  fault_inject_first=False,
                                  artifact_dir_root=Path(tmp))
            runner.assert_not_called()
            self.assertEqual(inc["status"], "remediation_failed")

    def test_verify_patch_disallowed_version_invokes_no_subprocess(self):
        """A matching requirements file is insufficient: the version itself
        must be allowed by the installer policy, and no subprocess runs."""
        with tempfile.TemporaryDirectory() as tmp:
            art = persist_artifact("inc-bad-version", "3.1.4", base_dir=Path(tmp))
            # Corrupt the artifact descriptor to claim a disallowed version.
            art["proposed_version"] = "9.9.9"
            # Ensure the file contents would have matched the wrong version
            # if we had not re-validated the version first.
            (Path(art["artifact_dir"]) / "requirements-proposed.txt").write_text(
                "jinja2==9.9.9\nMarkupSafe==2.1.5\npytest==8.3.3\n")
            result = verify_patch(art)
            self.assertFalse(result["success"])
            self.assertIn("version policy rejected", result["error"])
            self.assertFalse(result.get("install_ok", True))
            self.assertIsNone(result["pytest_returncode"])

    def test_verify_patch_fault_injection_descriptor_not_enabled_by_data(self):
        """A normal artifact descriptor with a 3.1.2 file cannot activate the
        fault-injection exception; the flag must come from trusted code."""
        with tempfile.TemporaryDirectory() as tmp:
            art = persist_artifact("inc-fault-flag", "3.1.4", base_dir=Path(tmp))
            art["proposed_version"] = "3.1.2"
            (Path(art["artifact_dir"]) / "requirements-proposed.txt").write_text(
                "jinja2==3.1.2\nMarkupSafe==2.1.5\npytest==8.3.3\n")
            result = verify_patch(art)
            self.assertFalse(result["success"])
            self.assertIn("version policy rejected", result["error"])


# ------------------------------------------------- 2. Artifact persistence

class TestArtifactPersistence(ArtifactTmpBase):

    def test_persist_artifact_contains_all_files(self):
        art = persist_artifact("inc-persist", "3.1.4", base_dir=Path(self._tmp))
        d = Path(art["artifact_dir"])
        for rel in ("requirements-original.txt", "requirements-proposed.txt",
                    "requirements.diff", "manifest.json",
                    "app/main.py", "app/test_regression.py", "app/pytest.ini"):
            self.assertTrue((d / rel).is_file(), rel)
        self.assertIn("jinja2==3.1.2", (d / "requirements-original.txt").read_text())
        self.assertIn("jinja2==3.1.4", (d / "requirements-proposed.txt").read_text())
        diff = (d / "requirements.diff").read_text()
        self.assertIn("-jinja2==3.1.2", diff)
        self.assertIn("+jinja2==3.1.4", diff)

    def test_manifest_is_canonical_over_actual_bytes(self):
        art = persist_artifact("inc-manifest", "3.1.4", base_dir=Path(self._tmp))
        m = compute_manifest(Path(art["artifact_dir"]))
        self.assertEqual(m["digest"], art["sha256"])
        # Recomputing is stable.
        self.assertEqual(compute_manifest(Path(art["artifact_dir"]))["digest"], m["digest"])
        # Digest binds file CONTENT, not just names.
        self.assertIn("app/main.py", m["files"])
        self.assertIn("requirements-proposed.txt", m["files"])

    def test_manifest_detects_app_code_mutation(self):
        art = persist_artifact("inc-mut-app", "3.1.4", base_dir=Path(self._tmp))
        target = Path(art["artifact_dir"]) / "app" / "main.py"
        target.write_text(target.read_text() + "\n# tampered\n")
        reason = manifest_matches(recompute_manifest(art["artifact_dir"]), art["manifest"])
        self.assertIsNotNone(reason)
        self.assertIn("changed", reason)

    def test_manifest_detects_test_file_mutation(self):
        art = persist_artifact("inc-mut-test", "3.1.4", base_dir=Path(self._tmp))
        target = Path(art["artifact_dir"]) / "app" / "test_regression.py"
        target.write_text("def test_fake():\n    assert True\n")
        reason = manifest_matches(recompute_manifest(art["artifact_dir"]), art["manifest"])
        self.assertIsNotNone(reason)
        self.assertIn("changed", reason)

    def test_manifest_detects_requirements_mutation(self):
        art = persist_artifact("inc-mut-req", "3.1.4", base_dir=Path(self._tmp))
        target = Path(art["artifact_dir"]) / "requirements-proposed.txt"
        target.write_text("jinja2==3.1.6\nMarkupSafe==2.1.5\n")
        reason = manifest_matches(recompute_manifest(art["artifact_dir"]), art["manifest"])
        self.assertIsNotNone(reason)
        self.assertIn("changed", reason)

    def test_manifest_detects_missing_file(self):
        art = persist_artifact("inc-miss", "3.1.4", base_dir=Path(self._tmp))
        (Path(art["artifact_dir"]) / "app" / "main.py").unlink()
        reason = manifest_matches(recompute_manifest(art["artifact_dir"]), art["manifest"])
        self.assertIsNotNone(reason)
        self.assertIn("missing", reason)

    def test_manifest_detects_extra_file(self):
        art = persist_artifact("inc-extra", "3.1.4", base_dir=Path(self._tmp))
        (Path(art["artifact_dir"]) / "app" / "evil.py").write_text("print('tampered')")
        reason = manifest_matches(recompute_manifest(art["artifact_dir"]), art["manifest"])
        self.assertIsNotNone(reason)
        self.assertIn("unexpected", reason)

    def test_manifest_missing_directory_fails_closed(self):
        m = recompute_manifest("/nonexistent/ns-artifact-dir")
        reason = manifest_matches(m, {"files": {"a": "b"}, "digest": "c"})
        self.assertIsNotNone(reason)
        self.assertIn("unreadable", reason)


# ------------------------------------------------- 3. JUnit parsing

class TestJUnitParsing(unittest.TestCase):

    def _write(self, xml):
        import xml.etree.ElementTree as ET
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            f.write(xml.encode())
            path = f.name
        self.addCleanup(os.unlink, path)
        return Path(path)

    def _junit(self, cases, failures=0, errors=0, skipped=0):
        body = "".join(
            f'<testcase name="{n}" classname="t">'
            + ("<failure message='x'/>" if s == "failed" else
               "<error message='x'/>" if s == "error" else
               "<skipped/>" if s == "skipped" else "")
            + "</testcase>"
            for n, s in cases)
        return (f'<?xml version="1.0"?><testsuites><testsuite tests="{len(cases)}" '
                f'failures="{failures}" errors="{errors}" skipped="{skipped}">{body}</testsuite></testsuites>')

    def test_parse_all_passed(self):
        cases = [(t, "passed") for t in REQUIRED_TESTS]
        ev = _parse_junit(self._write(self._junit(cases)))
        self.assertEqual(ev["failures"], 0)
        self.assertTrue(all(ev["tests"][t] == "passed" for t in REQUIRED_TESTS))

    def test_parse_failed_required_test(self):
        cases = [(t, "failed" if t == REQUIRED_LIST[0] else "passed") for t in REQUIRED_TESTS]
        ev = _parse_junit(self._write(self._junit(cases, failures=1)))
        self.assertEqual(ev["tests"][REQUIRED_LIST[0]], "failed")
        self.assertFalse(all(ev["tests"].get(t) == "passed" for t in REQUIRED_TESTS))

    def test_parse_skipped_required_test(self):
        cases = [(t, "skipped" if t == REQUIRED_LIST[1] else "passed") for t in REQUIRED_TESTS]
        ev = _parse_junit(self._write(self._junit(cases, skipped=1)))
        self.assertEqual(ev["tests"][REQUIRED_LIST[1]], "skipped")

    def test_parse_errored_required_test(self):
        cases = [(t, "error" if t == REQUIRED_LIST[2] else "passed") for t in REQUIRED_TESTS]
        ev = _parse_junit(self._write(self._junit(cases, errors=1)))
        self.assertEqual(ev["tests"][REQUIRED_LIST[2]], "error")

    def test_parse_missing_required_test(self):
        cases = [(t, "passed") for t in REQUIRED_TESTS if t != REQUIRED_LIST[3]]
        ev = _parse_junit(self._write(self._junit(cases)))
        self.assertNotIn(REQUIRED_LIST[3], ev["tests"])
        checks = evidence_mod.evaluate(make_verification(tests=ev["tests"]))
        self.assertFalse(checks["required_tests_all_passed"])

    def test_parse_duplicate_required_test(self):
        """A duplicated required test name must not silently pass as one."""
        cases = [(t, "passed") for t in REQUIRED_TESTS]
        cases.append((REQUIRED_LIST[0], "failed"))  # duplicate, failing
        ev = _parse_junit(self._write(self._junit(cases, failures=1)))
        self.assertEqual(ev["tests"][REQUIRED_LIST[0]], "error")  # poisoned

    def test_parse_garbage_xml(self):
        p = self._write("<not-xml")
        ev = _parse_junit(p)
        self.assertIn("parse_error", ev)

    def test_parse_rejects_bool_failure_count(self):
        """A boolean aggregate count must be rejected as malformed."""
        xml = ('<?xml version="1.0"?><testsuites><testsuite tests="1" '
               'failures="true" errors="0" skipped="0">'
               '<testcase name="test_x" classname="t"/></testsuite></testsuites>')
        ev = _parse_junit(self._write(xml))
        self.assertIn("parse_error", ev)

    def test_parse_rejects_float_error_count(self):
        xml = ('<?xml version="1.0"?><testsuites><testsuite tests="1" '
               'failures="0" errors="1.5" skipped="0">'
               '<testcase name="test_x" classname="t"/></testsuite></testsuites>')
        ev = _parse_junit(self._write(xml))
        self.assertIn("parse_error", ev)

    def test_parse_rejects_negative_skip_count(self):
        xml = ('<?xml version="1.0"?><testsuites><testsuite tests="1" '
               'failures="0" errors="0" skipped="-1">'
               '<testcase name="test_x" classname="t"/></testsuite></testsuites>')
        ev = _parse_junit(self._write(xml))
        self.assertIn("parse_error", ev)

    def test_parse_rejects_missing_failure_count(self):
        xml = ('<?xml version="1.0"?><testsuites><testsuite tests="1" '
               'errors="0" skipped="0">'
               '<testcase name="test_x" classname="t"><failure/></testcase>'
               '</testsuite></testsuites>')
        ev = _parse_junit(self._write(xml))
        self.assertIn("parse_error", ev)

    def test_parse_rejects_aggregate_count_inconsistency(self):
        """Reported totals contradicting derived testcase outcomes fail."""
        cases = [(t, "passed") for t in REQUIRED_TESTS]
        cases.append(("test_extra", "failed"))
        ev = _parse_junit(self._write(self._junit(cases, failures=0)))
        self.assertIn("parse_error", ev)
        self.assertIn("mismatch", ev["parse_error"])

    def test_parse_extra_failed_testcase_blocks_success(self):
        """A failed test outside the required four must still fail verification."""
        cases = [(t, "passed") for t in REQUIRED_TESTS]
        cases.append(("test_extra", "failed"))
        ev = _parse_junit(self._write(self._junit(cases, failures=1)))
        self.assertEqual(ev["tests"]["test_extra"], "failed")
        self.assertEqual(ev["failures"], 1)
        checks = evidence_mod.evaluate(make_verification(tests=ev["tests"]))
        self.assertFalse(checks["no_failures"])


# ------------------------------------------- 4. Shared evidence gates

class TestEvidenceGates(unittest.TestCase):

    def test_complete_passing_evidence(self):
        checks = evidence_mod.evaluate(make_verification())
        self.assertTrue(all(checks.values()), checks)

    def test_missing_fields_fail_closed(self):
        self.assertFalse(any(evidence_mod.evaluate(None).values()))
        self.assertFalse(any(evidence_mod.evaluate({}).values()))
        partial = make_verification()
        del partial["junit"]
        self.assertFalse(evidence_mod.evaluate(partial)["evidence_complete"])

    def test_absent_checks_map_never_establishes_success(self):
        """Evidence with a forged all-true deterministic_checks map but FAILED
        execution evidence must fail: checks are recomputed, not trusted."""
        v = make_verification(installed="3.1.2", proposed="3.1.2", rc=1,
                              tests={t: "failed" for t in REQUIRED_TESTS},
                              failures=4)
        v["deterministic_checks"] = {k: True for k in (
            "install_ok", "version_inspection_ok", "version_matches_pin",
            "version_satisfies_policy", "pytest_returncode_zero",
            "required_tests_all_passed", "no_failures", "no_errors", "no_skips")}
        checks = evidence_mod.evaluate(v)
        self.assertFalse(checks["version_satisfies_policy"])
        self.assertFalse(checks["pytest_returncode_zero"])
        self.assertFalse(checks["required_tests_all_passed"])
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_partial_checks_map_with_failed_evidence(self):
        """QA returns only some checks; failed execution evidence must still
        fail, and the recomputed map fills the rest."""
        v = make_verification(rc=1, failures=2,
                              tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                     **{t: "failed" for t in REQUIRED_LIST[2:]}})
        checks = evidence_mod.evaluate(v)
        self.assertFalse(checks["pytest_returncode_zero"])
        self.assertFalse(checks["no_failures"])
        self.assertFalse(checks["required_tests_all_passed"])

    def test_strict_types(self):
        v = make_verification(rc="0")  # string, not int
        self.assertFalse(evidence_mod.evaluate(v)["pytest_returncode_zero"])
        v = make_verification(rc=True)  # bool, not int
        self.assertFalse(evidence_mod.evaluate(v)["pytest_returncode_zero"])
        v = make_verification(installed=3.14)  # not a string
        self.assertFalse(evidence_mod.evaluate(v)["version_matches_pin"])

    def test_qa_recomputes_not_trusts(self):
        """QA verify() output checks must match the evaluator, ignoring any
        checks map embedded in the verification dict."""
        v = make_verification(rc=1, failures=2,
                              tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                     **{t: "failed" for t in REQUIRED_LIST[2:]}})
        v["deterministic_checks"] = {"install_ok": True}  # partial forged map
        qa = qa_verify(v, model_fn=good_model_fn)
        self.assertFalse(qa["approval"])  # deterministic veto overrides model
        self.assertEqual(qa["deterministic_checks"], evidence_mod.evaluate(v))

    def test_model_approval_cannot_override_deterministic_failure(self):
        v = make_verification(installed="3.1.2", proposed="3.1.2", rc=1,
                              failures=2)
        qa = qa_verify(v, model_fn=good_model_fn)
        self.assertFalse(qa["approval"])
        self.assertIn("Deterministic evidence checks failed", qa["reason"])

    def test_good_evidence_qa_approves(self):
        qa = qa_verify(make_verification(), model_fn=good_model_fn)
        self.assertTrue(qa["approval"])


# --------------------------------------- 5. Workflow-level mock scenarios

class TestWorkflowScenarios(ArtifactTmpBase):

    def test_full_workflow_verified(self):
        art = persist_artifact("inc-full", "3.1.4", base_dir=Path(self._tmp))
        runner = MagicMock(return_value=make_verification(artifact=art))
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        runner.assert_called_once()
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_approval")
        self.assertIsNotNone(inc["verified_patch"]["artifact_dir"])
        self.assertIsNotNone(inc["verified_patch"]["manifest"])
        self.assertIn("jinja2==3.1.4", inc["verified_patch"]["requirements_diff"])

    def test_qa_true_with_invalid_evidence_leaves_failed(self):
        """QA approval True is not enough; execution evidence must also pass
        for the orchestrator to set awaiting-human-approval."""
        bad_qa = lambda v: {"approval": True, "reason": "forged", "deterministic_checks": {}}
        runner = MagicMock(return_value=make_verification(rc=1, failures=2,
                                                           tests={t: "failed" for t in REQUIRED_TESTS}))
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4", "3.1.5"]),
                                runner_fn=runner,
                                qa_fn=bad_qa,
                                fault_inject_first=False)
        self.assertEqual(inc["status"], "remediation_failed")
        self.assertIsNone(inc["elapsed_alert_to_tested_patch_ms"])
        self.assertNotIn("verified_patch", inc)

    def test_uuid_incident_id(self):
        import uuid
        runner = MagicMock(return_value=make_verification())
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        try:
            uuid.UUID(inc["id"])
        except ValueError:
            self.fail("incident id is not a valid UUID")

    def test_needs_human_review_blocked(self):
        """NEEDS_HUMAN_REVIEW disposition produces no patch attempts."""
        inc = self.run_incident(sentry_fn=make_sentry("NEEDS_HUMAN_REVIEW"),
                                fault_inject_first=False,
                                engineer_fn=make_engineer(["3.1.4"]))
        self.assertEqual(inc["status"], "blocked")
        self.assertEqual(inc["attempts"], [])
        self.assertIsNone(inc["elapsed_alert_to_tested_patch_ms"])

    def test_model_false_string_rejected(self):
        """A model returning the string 'false' (not JSON false) is malformed
        and never approval."""
        def string_false_fn(system_prompt, user_prompt):
            return {"success": True, "response": {"approval": "false", "reason": "looks false"},
                    "raw": '{"approval":"false"}', "elapsed_ms": 1.0, "error": None}
        runner = MagicMock(return_value=make_verification())
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=string_false_fn),
                                fault_inject_first=False)
        self.assertEqual(inc["status"], "remediation_failed")
        self.assertFalse(inc["attempts"][0]["qa"]["approval"])
        self.assertIn("Malformed model output", inc["attempts"][0]["qa"]["reason"])

    def test_fault_injection_reaches_installer_and_rejected(self):
        runner = MagicMock(side_effect=[
            make_verification(installed="3.1.2", proposed="3.1.2", failures=2,
                              rc=1, tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                           **{t: "failed" for t in REQUIRED_LIST[2:]}}),
            make_verification(),
        ])
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn))
        self.assertEqual(runner.call_count, 2)
        first = inc["attempts"][0]
        self.assertTrue(first["engineer"]["fault_injection"])
        self.assertTrue(first["verification"]["installer_invoked"])
        self.assertFalse(first["qa"]["approval"])
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_approval")

    def test_both_attempts_rejected(self):
        runner = MagicMock(return_value=make_verification(rc=1, failures=4,
                                                           tests={t: "failed" for t in REQUIRED_TESTS}))
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4", "3.1.5"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        self.assertEqual(inc["status"], "remediation_failed")
        self.assertIsNone(inc["elapsed_alert_to_tested_patch_ms"])
        self.assertNotIn("verified_patch", inc)

    def test_blocked_incident_no_attempts(self):
        inc = self.run_incident(sentry_fn=make_sentry("BENIGN"), fault_inject_first=False,
                                engineer_fn=make_engineer(["3.1.4"]))
        self.assertEqual(inc["status"], "blocked")
        self.assertEqual(inc["attempts"], [])

    def test_engineer_rejection_prompt_uses_current_fields(self):
        """Engineer's second call must receive pytest_returncode, junit, and
        deterministic_checks in the rejection payload."""
        captured = {}

        def engineer_fn(ctx, prev=None):
            if prev is not None:
                captured.update(prev)
                return {"proposal": {"package": "jinja2", "version": "3.1.4", "reason": "retry"},
                        "reason": "retry", "model": "mock", "elapsed_ms": 1.0,
                        "utc_timestamp": "t", "error": None, "raw": "{}"}
            return {"proposal": {"package": "jinja2", "version": "3.1.5", "reason": "first"},
                    "reason": "first", "model": "mock", "elapsed_ms": 1.0,
                    "utc_timestamp": "t", "error": None, "raw": "{}"}

        runner = MagicMock(side_effect=[
            make_verification(rc=1, failures=2, proposed="3.1.5", installed="3.1.5",
                              tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                     **{t: "failed" for t in REQUIRED_LIST[2:]}}),
            make_verification(),
        ])
        inc = self.run_incident(engineer_fn=engineer_fn, runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        self.assertEqual(runner.call_count, 2)
        self.assertIn("pytest_returncode", captured)
        self.assertEqual(captured["pytest_returncode"], 1)
        self.assertIn("junit", captured)
        self.assertEqual(captured["junit"]["failures"], 2)
        self.assertIn("deterministic_checks", captured)
        self.assertFalse(captured["deterministic_checks"]["pytest_returncode_zero"])
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_ability")

class TestWorkflowScenarios(ArtifactTmpBase):

    def test_full_workflow_verified(self):
        art = persist_artifact("inc-full", "3.1.4", base_dir=Path(self._tmp))
        runner = MagicMock(return_value=make_verification(artifact=art))
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        runner.assert_called_once()
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_approval")
        self.assertIsNotNone(inc["verified_patch"]["artifact_dir"])
        self.assertIsNotNone(inc["verified_patch"]["manifest"])
        self.assertIn("jinja2==3.1.4", inc["verified_patch"]["requirements_diff"])

    def test_fault_injection_reaches_installer_and_rejected(self):
        runner = MagicMock(side_effect=[
            make_verification(installed="3.1.2", proposed="3.1.2", failures=2,
                              rc=1, tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                           **{t: "failed" for t in REQUIRED_LIST[2:]}}),
            make_verification(),
        ])
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn))
        self.assertEqual(runner.call_count, 2)
        first = inc["attempts"][0]
        self.assertTrue(first["engineer"]["fault_injection"])
        self.assertTrue(first["verification"]["installer_invoked"])
        self.assertFalse(first["qa"]["approval"])
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_approval")

    def test_both_attempts_rejected(self):
        runner = MagicMock(return_value=make_verification(rc=1, failures=4,
                                                           tests={t: "failed" for t in REQUIRED_TESTS}))
        inc = self.run_incident(engineer_fn=make_engineer(["3.1.4", "3.1.5"]),
                                runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        self.assertEqual(inc["status"], "remediation_failed")
        self.assertIsNone(inc["elapsed_alert_to_tested_patch_ms"])
        self.assertNotIn("verified_patch", inc)

    def test_blocked_incident_no_attempts(self):
        inc = self.run_incident(sentry_fn=make_sentry("BENIGN"), fault_inject_first=False,
                                engineer_fn=make_engineer(["3.1.4"]))
        self.assertEqual(inc["status"], "blocked")
        self.assertEqual(inc["attempts"], [])

    def test_engineer_rejection_prompt_uses_current_fields(self):
        """Engineer's second call must receive pytest_returncode, junit, and
        deterministic_checks in the rejection payload."""
        captured = {}

        def engineer_fn(ctx, prev=None):
            if prev is not None:
                captured.update(prev)
                return {"proposal": {"package": "jinja2", "version": "3.1.4", "reason": "retry"},
                        "reason": "retry", "model": "mock", "elapsed_ms": 1.0,
                        "utc_timestamp": "t", "error": None, "raw": "{}"}
            return {"proposal": {"package": "jinja2", "version": "3.1.5", "reason": "first"},
                    "reason": "first", "model": "mock", "elapsed_ms": 1.0,
                    "utc_timestamp": "t", "error": None, "raw": "{}"}

        runner = MagicMock(side_effect=[
            make_verification(rc=1, failures=2, proposed="3.1.5", installed="3.1.5",
                              tests={**{t: "passed" for t in REQUIRED_LIST[:2]},
                                     **{t: "failed" for t in REQUIRED_LIST[2:]}}),
            make_verification(),
        ])
        inc = self.run_incident(engineer_fn=engineer_fn, runner_fn=runner,
                                qa_fn=lambda v: qa_verify(v, model_fn=good_model_fn),
                                fault_inject_first=False)
        self.assertEqual(runner.call_count, 2)
        self.assertIn("pytest_returncode", captured)
        self.assertEqual(captured["pytest_returncode"], 1)
        self.assertIn("junit", captured)
        self.assertEqual(captured["junit"]["failures"], 2)
        self.assertIn("deterministic_checks", captured)
        self.assertFalse(captured["deterministic_checks"]["pytest_returncode_zero"])
        self.assertEqual(inc["status"], "tested_patch_awaiting_human_approval")


# ------------------------------------------------- 6. Approval gates

class TestApprovalGates(ArtifactTmpBase):

    def _fail_evidence(self, v):
        """Mutate the real evidence fields to a failing state."""
        v["pytest_returncode"] = 1
        v["junit"] = {"tests": {**{t: "passed" for t in REQUIRED_LIST[:2]},
                                **{t: "failed" for t in REQUIRED_LIST[2:]}},
                      "total": len(REQUIRED_TESTS), "failures": 2, "errors": 0, "skipped": 0}

    def _verified_incident(self):
        art = persist_artifact("inc-approval", "3.1.4", base_dir=Path(self._tmp))
        verification = make_verification(artifact=art)
        verification["deterministic_checks"] = evidence_mod.evaluate(verification)
        attempt = {
            "attempt": 1,
            "engineer": {"proposal": {"package": "jinja2", "version": "3.1.4", "reason": "r"}},
            "verification": verification,
            "qa": {"approval": True, "reason": "ok",
                   "deterministic_checks": evidence_mod.evaluate(verification)},
        }
        return {
            "id": "inc-approval", "status": "tested_patch_awaiting_human_approval",
            "attempts": [attempt], "events": [],
            "verified_patch": {
                "package": "jinja2", "version": "3.1.4",
                "artifact_sha256": verification["artifact_sha256"],
                "artifact_dir": art["artifact_dir"],
                "manifest": art["manifest"],
                "installed_version": "3.1.4",
            },
            "elapsed_alert_to_tested_patch_ms": 100.0,
        }

    def test_approval_succeeds_on_intact_artifact(self):
        inc = self._verified_incident()
        res = ap.approve(inc)
        self.assertTrue(res["approved"], res)
        self.assertEqual(inc["status"], "human_approved")

    def test_approval_rejects_missing_artifact_file(self):
        inc = self._verified_incident()
        (Path(inc["verified_patch"]["artifact_dir"]) / "app" / "test_regression.py").unlink()
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("manifest recheck failed", res["error"])
        self.assertIn("missing", res["error"])

    def test_approval_rejects_mutated_requirements(self):
        inc = self._verified_incident()
        p = Path(inc["verified_patch"]["artifact_dir"]) / "requirements-proposed.txt"
        p.write_text(p.read_text() + "\nevil==1.0\n")
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("manifest recheck failed", res["error"])

    def test_approval_rejects_mutated_app_code(self):
        inc = self._verified_incident()
        p = Path(inc["verified_patch"]["artifact_dir"]) / "app" / "main.py"
        p.write_text(p.read_text() + "\n# tampered\n")
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("changed", res["error"])

    def test_approval_rejects_missing_artifact_dir(self):
        inc = self._verified_incident()
        shutil.rmtree(inc["verified_patch"]["artifact_dir"])
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("manifest recheck failed", res["error"])

    def test_approval_rejects_forged_qa_checks_with_failed_evidence(self):
        """QA map says all-pass, execution evidence fails: approval must
        recompute and reject."""
        inc = self._verified_incident()
        v = inc["attempts"][0]["verification"]
        self._fail_evidence(v)
        inc["attempts"][0]["qa"]["deterministic_checks"] = {
            "install_ok": True, "version_inspection_ok": True,
            "version_matches_pin": True, "version_satisfies_policy": True,
            "pytest_returncode_zero": True, "required_tests_all_passed": True,
            "no_failures": True, "no_errors": True, "no_skips": True}
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("Recomputed deterministic evidence checks", res["error"])

    def test_approval_rejects_missing_checks_map_with_failed_evidence(self):
        inc = self._verified_incident()
        v = inc["attempts"][0]["verification"]
        self._fail_evidence(v)
        inc["attempts"][0]["qa"]["deterministic_checks"] = {}
        res = ap.approve(inc)
        self.assertFalse(res["approved"])
        self.assertIn("Recomputed deterministic evidence checks", res["error"])

    def test_approval_rejects_wrong_status_and_repeat(self):
        inc = self._verified_incident()
        inc["status"] = "remediation_failed"
        self.assertFalse(ap.approve(inc)["approved"])
        inc["status"] = "tested_patch_awaiting_human_approval"
        self.assertTrue(ap.approve(inc)["approved"])
        self.assertFalse(ap.approve(inc)["approved"])  # non-transferable

    def test_deploy_after_approval_and_mutation_blocks_release(self):
        inc = self._verified_incident()
        self.assertTrue(ap.approve(inc)["approved"])
        res = ap.simulate_deploy(inc)
        self.assertTrue(res["simulated"], res)

        # A second incident whose artifact mutates AFTER approval:
        inc2 = self._verified_incident()
        inc2["id"] = "inc-approval-2"
        self.assertTrue(ap.approve(inc2)["approved"])
        p = Path(inc2["verified_patch"]["artifact_dir"]) / "app" / "main.py"
        p.write_text(p.read_text() + "\n# tampered after approval\n")
        res2 = ap.simulate_deploy(inc2)
        self.assertFalse(res2["simulated"])
        self.assertIn("manifest recheck failed", res2["error"])

    def test_deploy_requires_approval(self):
        inc = self._verified_incident()
        res = ap.simulate_deploy(inc)
        self.assertFalse(res["simulated"])

    def test_deploy_blocked_when_artifact_dir_deleted(self):
        inc = self._verified_incident()
        self.assertTrue(ap.approve(inc)["approved"])
        shutil.rmtree(inc["verified_patch"]["artifact_dir"])
        res = ap.simulate_deploy(inc)
        self.assertFalse(res["simulated"])
        self.assertIn("manifest recheck failed", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
