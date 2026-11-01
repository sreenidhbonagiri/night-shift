"""Focused mock tests for the real local staging release module.

No model calls. Environment build, process start, and HTTP fetches are
replaced with injectable mocks (staging._make_env, staging._start,
staging._get_json). The release gates, health/smoke validation, duplicate
prevention, affected-version rejection, artifact-mutation rejection, and
cleanup-on-failure logic are exercised for real.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import staging
from app import evidence as evidence_mod
from app.isolated_runner import persist_artifact
from app.approval import approve


REQUIRED_TESTS = {
    "test_render_greeting_escapes_html",
    "test_xmlattr_renders_normal_attributes",
    "test_xmlattr_rejects_space_in_key",
    "test_xmlattr_rejects_slash_in_key",
}


def make_verification(installed="3.1.4", proposed="3.1.4", rc=0,
                      tests=None, failures=0, errors=0, skipped=0,
                      install_ok=True, inspection_ok=True, sha="a" * 64,
                      artifact=None):
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


class StagingBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging_root = self.root / "staging"
        # Clean the process registries between tests.
        staging._processes.clear()
        staging._staging_dirs.clear()

    def make_incident(self, version="3.1.4", approved=True):
        art = persist_artifact("inc-test", version, base_dir=self.root / "artifacts")
        verification = make_verification(artifact=art, installed=version,
                                         proposed=version)
        verification["deterministic_checks"] = evidence_mod.evaluate(verification)
        attempt = {
            "attempt": 1,
            "engineer": {"proposal": {"package": "jinja2", "version": version,
                                      "reason": "r"}},
            "verification": verification,
            "qa": {"approval": True, "reason": "ok",
                   "deterministic_checks": evidence_mod.evaluate(verification)},
        }
        incident = {
            "id": "inc-test", "status": "tested_patch_awaiting_human_approval",
            "attempts": [attempt], "events": [],
            "verified_patch": {
                "package": "jinja2", "version": version,
                "artifact_sha256": verification["artifact_sha256"],
                "artifact_dir": art["artifact_dir"],
                "manifest": art["manifest"],
                "installed_version": version,
            },
            "elapsed_alert_to_tested_patch_ms": 100.0,
        }
        if approved:
            res = approve(incident)
            assert res["approved"], res
        return incident, art

    def mock_env_ok(self):
        return patch.object(staging, "_make_env", return_value="/fake/venv/python")

    def mock_process(self):
        proc = MagicMock()
        proc.poll.return_value = None
        return patch.object(staging, "_start", return_value=proc), proc

    def mock_http(self, health=None, smoke=None):
        health = health or {"status": "ok", "jinja2": "3.1.4",
                            "MarkupSafe": "2.1.5"}
        smoke = smoke or {"status": "ok", "checks": {
            "greeting_escapes_html": True, "xmlattr_rejects_space": True,
            "xmlattr_rejects_slash": True}}
        return patch.object(staging, "_get_json",
                            side_effect=[health, smoke])


class TestReleaseGates(StagingBase):

    def test_release_requires_approval(self):
        inc, _ = self.make_incident(approved=False)
        with self.mock_env_ok():
            res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("human approval", res["error"])

    def test_release_rejects_fault_injection_version(self):
        """The affected 3.1.2 artifact can never be released, even with a
        forged approval + human_approved status."""
        # 3.1.2 only persists via the trusted fault-injection flag.
        art = persist_artifact("inc-test", "3.1.2", fault_injection=True,
                               base_dir=self.root / "artifacts")
        verification = make_verification(artifact=art, installed="3.1.2",
                                         proposed="3.1.2")
        incident = {
            "id": "inc-test", "status": "human_approved",
            "attempts": [{
                "attempt": 1,
                "engineer": {"proposal": {"package": "jinja2",
                                          "version": "3.1.2", "reason": "r"}},
                "verification": verification,
                "qa": {"approval": True, "reason": "forged"},
            }],
            "events": [],
            "verified_patch": {
                "package": "jinja2", "version": "3.1.2",
                "artifact_sha256": verification["artifact_sha256"],
                "artifact_dir": art["artifact_dir"],
                "manifest": art["manifest"],
                "installed_version": "3.1.2",
            },
            "human_approval": {
                "approved": True, "actor": "forged",
                "patch": {"package": "jinja2", "version": "3.1.2"},
                "artifact_sha256": verification["artifact_sha256"],
            },
        }
        with self.mock_env_ok():
            res = staging.release(incident, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("3.1.2", res["error"])
        self.assertIn("never", res["error"])
        self.assertFalse((self.staging_root / "inc-test").exists())

    def test_release_rejects_approval_version_mismatch(self):
        inc, _ = self.make_incident()
        inc["human_approval"]["patch"]["version"] = "3.1.5"
        with self.mock_env_ok():
            res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("does not match", res["error"])

    def test_release_rejects_mutated_artifact(self):
        inc, art = self.make_incident()
        p = Path(art["artifact_dir"]) / "app" / "main.py"
        p.write_text(p.read_text() + "\n# tampered\n")
        with self.mock_env_ok():
            res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("manifest recheck", res["error"])

    def test_release_rejects_failed_evidence(self):
        inc, _ = self.make_incident()
        v = inc["attempts"][0]["verification"]
        v["pytest_returncode"] = 1
        v["junit"]["tests"]["test_xmlattr_rejects_space_in_key"] = "failed"
        with self.mock_env_ok():
            res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("evidence checks failed", res["error"])

    def test_release_rejects_not_found_incident(self):
        res = staging.release(None)
        self.assertFalse(res["released"])

    def test_release_prevents_duplicates(self):
        inc, _ = self.make_incident()
        with self.mock_env_ok(), self.mock_process()[0] as p, self.mock_http():
            res = staging.release(inc, staging_root=self.staging_root)
            self.assertTrue(res["released"], res)
            # Second release attempt for the same incident must be rejected.
            res2 = staging.release(inc, staging_root=self.staging_root)
            self.assertFalse(res2["released"])
            self.assertIn("already", res2["error"])
        staging._processes.clear(); staging._staging_dirs.clear()


class TestHealthValidation(StagingBase):

    def _release_with_health(self, health, smoke=None):
        inc, _ = self.make_incident()
        with self.mock_env_ok():
            pm, proc = self.mock_process()
            with pm, self.mock_http(health=health, smoke=smoke):
                res = staging.release(inc, staging_root=self.staging_root)
        return res, inc, proc

    def test_health_wrong_jinja2_version_fails_closed(self):
        res, inc, proc = self._release_with_health(
            {"status": "ok", "jinja2": "3.1.3", "MarkupSafe": "2.1.5"})
        self.assertFalse(res["released"])
        self.assertIn("health", res["error"])
        # Failure path killed the process and cleaned up.
        proc.terminate.assert_called_once()
        self.assertFalse((self.staging_root / "inc-test").exists())
        self.assertNotIn("inc-test", staging._processes)
        self.assertNotIn("inc-test", staging._staging_dirs)

    def test_health_wrong_markupsafe_version_fails_closed(self):
        res, _, _ = self._release_with_health(
            {"status": "ok", "jinja2": "3.1.4", "MarkupSafe": "2.0.1"})
        self.assertFalse(res["released"])
        self.assertIn("MarkupSafe", res["error"])

    def test_health_bad_status_fails_closed(self):
        res, _, _ = self._release_with_health(
            {"status": "degraded", "jinja2": "3.1.4", "MarkupSafe": "2.1.5"})
        self.assertFalse(res["released"])
        self.assertIn("status", res["error"])

    def test_malformed_health_json_fails_closed(self):
        """Non-dict health payloads (strings, lists) fail closed."""
        res, _, _ = self._release_with_health("ok")
        self.assertFalse(res["released"])
        self.assertIn("not a JSON object", res["error"])

    def test_smoke_failure_fails_closed_and_cleans_up(self):
        res, inc, proc = self._release_with_health(
            {"status": "ok", "jinja2": "3.1.4", "MarkupSafe": "2.1.5"},
            smoke={"status": "failed", "checks": {"xmlattr_rejects_space": False}})
        self.assertFalse(res["released"])
        self.assertIn("smoke", res["error"])
        self.assertFalse((self.staging_root / "inc-test").exists())
        self.assertNotIn("staging_release", inc)

    def test_server_exit_during_startup_fails_closed(self):
        inc, _ = self.make_incident()
        proc = MagicMock()
        proc.poll.return_value = 1  # already dead
        proc.returncode = 1
        with self.mock_env_ok():
            with patch.object(staging, "_start", return_value=proc):
                res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("exited during startup", res["error"])
        self.assertFalse((self.staging_root / "inc-test").exists())

    def test_env_build_failure_fails_closed_and_cleans_up(self):
        inc, _ = self.make_incident()
        with patch.object(staging, "_make_env", return_value=None):
            res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        self.assertIn("environment build failed", res["error"])
        self.assertFalse(self.staging_root.exists() and any(self.staging_root.iterdir()))


class TestSuccessPath(StagingBase):

    def test_successful_release_records_and_stops(self):
        inc, _ = self.make_incident()
        with self.mock_env_ok():
            pm, proc = self.mock_process()
            with pm, self.mock_http():
                res = staging.release(inc, staging_root=self.staging_root)
        self.assertTrue(res["released"], res)
        self.assertEqual(inc["status"], "staging_released")
        self.assertEqual(res["version"], "3.1.4")
        self.assertEqual(res["health"]["jinja2"], "3.1.4")
        self.assertTrue(res["running"])
        self.assertEqual(inc["status"], "staging_released")

        # Status endpoint data: running, port present.
        st = staging.status(inc)
        self.assertTrue(st["running"])
        self.assertIsNotNone(st["port"])

        # Stop: process killed, staging dir removed, record updated.
        stop_res = staging.stop(inc)
        self.assertTrue(stop_res["stopped"])
        proc.terminate.assert_called_once()
        self.assertFalse((self.staging_root / "inc-test").exists())
        self.assertFalse(inc["staging_release"]["running"])
        self.assertIn("stopped_utc_timestamp", inc["staging_release"])

        # Second stop is rejected.
        stop2 = staging.stop(inc)
        self.assertFalse(stop2["stopped"])

        # Incident stays JSON-serializable (no Popen objects inside).
        import json
        json.dumps(inc)

        # Release events are recorded for export.
        actions = [e["action"] for e in inc["events"] if e.get("agent") == "staging"]
        self.assertIn("release_succeeded", actions)
        self.assertIn("stop", actions)

    def test_failed_release_records_event_for_export(self):
        inc, _ = self.make_incident(approved=False)
        res = staging.release(inc, staging_root=self.staging_root)
        self.assertFalse(res["released"])
        actions = [e["action"] for e in inc["events"] if e.get("agent") == "staging"]
        self.assertIn("release_failed", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
