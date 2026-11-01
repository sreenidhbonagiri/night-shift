"""Regression tests closing the Git trust-boundary gaps.

Independently reproduced failure (pre-repair): verify_checkout with
proposed_version=3.1.4 and a measured installed version of 3.1.2 returned
verified=True with passing tests — the measured version was never compared
to the approved pin.

These tests are written BEFORE the repair:
1. Committed requirements are validated by the strict validator before any
   install subprocess runs (disallowed lines reject pre-install).
2. Measured Jinja2 version must equal the approved pin; measured
   supporting dependencies must equal their exact pins.
3. Git-bound approval and release fail closed: missing proposal, missing
   approval commit/tree, failed verification, or proposal error all reject
   before _make_env/_start, with no fallback to mutable identifiers.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import git_proposal as gp
from app import staging as staging_mod
from app import approval as approval_mod
from app.isolated_runner import REMEDIATION_PACKAGE, SUPPORTING_PINS
from tests.test_git_proposal_repair import _make_artifact


class TestRequirementsValidatedBeforeInstall(unittest.TestCase):
    """Committed requirements must pass the strict validator before pip runs."""

    def setUp(self):
        self.incident_id = "gate-req-001"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def _proposal(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256req", {})
        self.assertNotIn("error", prop)
        return prop

    def test_disallowed_requirements_reject_before_install(self):
        prop = self._proposal()
        repo = Path(prop["repo_dir"])
        # Commit a disallowed requirements file (URL line) to the branch.
        (repo / "requirements.txt").write_text(
            "jinja2==3.1.4\ngit+https://example.invalid/evil.git\n")
        gp._git(repo, "add", "-A")
        gp._git(repo, "commit", "-m", "inject url line")
        new_head = gp._git(repo, "rev-parse", "HEAD").stdout.strip()
        prop["proposal_commit"] = new_head
        prop["proposal_tree"] = gp._git(
            repo, "rev-parse", "HEAD^{tree}").stdout.strip()

        install_calls = []
        real_run = gp._run

        def spy(cmd, cwd, timeout=None):
            if any("pip" in str(c) for c in cmd):
                install_calls.append(cmd)
            return real_run(cmd, cwd, timeout=timeout)

        with patch("app.git_proposal._run", side_effect=spy):
            result = gp.verify_checkout(prop)
        self.assertFalse(result["verified"])
        self.assertIn("requirements", (result["error"] or "").lower())
        self.assertEqual(install_calls, [],
                         "pip must never run on disallowed requirements")

    def test_wrong_supporting_pin_rejected(self):
        prop = self._proposal()
        repo = Path(prop["repo_dir"])
        (repo / "requirements.txt").write_text(
            "jinja2==3.1.4\nMarkupSafe==2.1.3\n")
        gp._git(repo, "add", "-A")
        gp._git(repo, "commit", "-m", "wrong supporting pin")
        prop["proposal_commit"] = gp._git(repo, "rev-parse", "HEAD").stdout.strip()
        prop["proposal_tree"] = gp._git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
        result = gp.verify_checkout(prop)
        self.assertFalse(result["verified"])
        self.assertIn("requirements", (result["error"] or "").lower())


class TestMeasuredVersionMustMatchPin(unittest.TestCase):
    """Measured installed versions must equal the approved exact pins."""

    def setUp(self):
        self.incident_id = "gate-ver-002"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def _proposal(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256ver", {})
        self.assertNotIn("error", prop)
        return prop

    def test_measured_jinja2_mismatch_rejects(self):
        """Independently reproduced gap: measured 3.1.2 with passing tests
        must NOT verify a 3.1.4 proposal."""
        prop = self._proposal()
        real_run = gp._run

        def fake_run(cmd, cwd, timeout=None):
            # Intercept the Jinja2 version-inspection command and lie: the
            # environment "actually" has 3.1.2.
            if ("version('%s')" % REMEDIATION_PACKAGE) in " ".join(map(str, cmd)):
                return 0, "3.1.2\n", ""
            return real_run(cmd, cwd, timeout=timeout)

        with patch("app.git_proposal._run", side_effect=fake_run):
            result = gp.verify_checkout(prop)
        self.assertFalse(result["verified"],
                         "measured 3.1.2 must reject a 3.1.4 proposal")
        self.assertIn("version", (result["error"] or "").lower())
        self.assertIsNotNone(result["error"])

    def test_measured_supporting_pin_mismatch_rejects(self):
        prop = self._proposal()
        real_run = gp._run

        def fake_run(cmd, cwd, timeout=None):
            if "version('MarkupSafe')" in " ".join(map(str, cmd)):
                return 0, "9.9.9\n", ""
            return real_run(cmd, cwd, timeout=timeout)

        with patch("app.git_proposal._run", side_effect=fake_run):
            result = gp.verify_checkout(prop)
        self.assertFalse(result["verified"])
        self.assertIn("markupsafe", (result["error"] or "").lower())

    def test_valid_exact_pins_still_succeed(self):
        prop = self._proposal()
        result = gp.verify_checkout(prop)
        self.assertTrue(result["verified"],
                        f"valid exact-pin verification failed: {result.get('error')}")
        self.assertEqual(result["installed_version"], "3.1.4")
        self.assertEqual(result["installed_version_source"], "measured")


class _GitBoundIncidentBase(unittest.TestCase):
    """Shared construction: a genuinely verified, approved Git incident."""

    incident_id = "gate-shared"

    def setUp(self):
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def _approved_incident(self):
        from app.isolated_runner import _write_manifest
        manifest = _write_manifest(Path(self.art))
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  manifest["digest"], manifest)
        self.assertNotIn("error", prop)
        ver = gp.verify_checkout(prop)
        self.assertTrue(ver["verified"], ver.get("error"))
        incident = {
            "id": self.incident_id,
            "status": "tested_patch_awaiting_human_approval",
            "events": [],
            "git_proposal": {**prop, **ver},
            "attempts": [{
                "qa": {"approval": True},
                "engineer": {"proposal": {"package": "jinja2", "version": "3.1.4"}},
                "verification": {
                    "artifact_sha256": manifest["digest"],
                    "artifact_dir": self.art,
                    "manifest": manifest,
                    "install_ok": True,
                    "version_inspection_ok": True,
                    "installed_version": "3.1.4",
                    "proposed_version": "3.1.4",
                    "installed_dependency_versions": {"MarkupSafe": "2.1.5"},
                    "pytest_returncode": 0,
                    "junit": {"tests": {
                        "test_render_greeting_escapes_html": "passed",
                        "test_xmlattr_renders_normal_attributes": "passed",
                        "test_xmlattr_rejects_space_in_key": "passed",
                        "test_xmlattr_rejects_slash_in_key": "passed"},
                        "total": 4, "failures": 0, "errors": 0, "skipped": 0},
                    "required_tests_ok": True,
                    "success": True,
                    "installer_invoked": True,
                    "requirements_validated": True,
                    "test_output": "4 passed",
                }}],
            "verified_patch": {
                "package": "jinja2", "version": "3.1.4",
                "proposal": {"package": "jinja2", "version": "3.1.4"},
                "artifact_sha256": manifest["digest"],
                "artifact_dir": self.art, "manifest": manifest,
            },
        }
        record = approval_mod.approve(incident)
        self.assertTrue(record.get("approved"), record.get("error"))
        return incident, prop


class TestReleaseFailsClosedOnGitState(_GitBoundIncidentBase):
    """Git-bound release must reject missing/failed/error states before
    _make_env or _start."""

    incident_id = "gate-release-003"

    def _assert_rejects_without_side_effects(self, incident, needle):
        env_calls, start_calls = [], []
        with patch.object(staging_mod, "_make_env",
                          side_effect=lambda *a, **k: env_calls.append(1) or None), \
             patch.object(staging_mod, "_start",
                          side_effect=lambda *a, **k: start_calls.append(1) or None):
            result = staging_mod.release(incident)
        self.assertFalse(result.get("released"))
        self.assertIn(needle, (result.get("error") or "").lower())
        self.assertEqual(env_calls, [])
        self.assertEqual(start_calls, [])

    def test_clean_approved_incident_still_reaches_staging(self):
        # Gate must pass; we only check the gate (release would create a
        # real env/process otherwise).
        incident, _ = self._approved_incident()
        self.assertIsNone(staging_mod._release_gate(incident))

    def test_missing_proposal_rejects(self):
        incident, _ = self._approved_incident()
        incident["git_proposal"] = None
        self._assert_rejects_without_side_effects(incident, "git")

    def test_missing_approval_git_binding_rejects(self):
        incident, _ = self._approved_incident()
        incident["human_approval"].pop("git", None)
        self._assert_rejects_without_side_effects(incident, "git")

    def test_failed_verification_rejects(self):
        incident, _ = self._approved_incident()
        incident["git_proposal"]["verified"] = False
        self._assert_rejects_without_side_effects(incident, "git")

    def test_proposal_error_rejects(self):
        incident, _ = self._approved_incident()
        incident["git_proposal"]["error"] = "stale verification failure"
        self._assert_rejects_without_side_effects(incident, "git")

    def test_no_mutable_fallback_at_release(self):
        """Approval-record commit replaced with 'wrong' must reject even if
        the mutable proposal descriptor matches the checkout."""
        incident, prop = self._approved_incident()
        incident["human_approval"]["git"]["commit"] = "0" * 40
        self._assert_rejects_without_side_effects(incident, "git")


if __name__ == "__main__":
    unittest.main(verbosity=2)
