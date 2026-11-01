"""Regression tests exposing the checkpoint-27 repair gaps.

Written BEFORE the repairs. Each test targets a specific inspected failure:
1. verify_checkout must run genuine isolated verification (not host python,
   not copying proposed_version into installed_version).
2. create_proposal must commit the artifact's exact requirement bytes and
   snapshot files (app/ layout), failing when required files are absent.
3. Workflow must block approval when Git delivery fails verification.
4. approval must bind commit/tree; staging must compare against the
   approval record.
5. Mutation of the Git checkout must be rejected by staging.release with a
   Git-specific reason before any environment creation or process start.
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
from app.isolated_runner import REMEDIATION_PACKAGE, SUPPORTING_PINS


_REAL_TESTS = '''"""Real regression tests matching REQUIRED_TESTS."""
import jinja2

from main import render_greeting, attr_filter


def test_render_greeting_escapes_html():
    assert "<script>" not in render_greeting("<script>x</script>")

def test_xmlattr_renders_normal_attributes():
    out = attr_filter({"class": "a"})
    assert "class" in out

def test_xmlattr_rejects_space_in_key():
    try:
        attr_filter({"bad key": "v"})
        raised = False
    except (ValueError, jinja2.exceptions.SecurityError):
        raised = True
    assert raised

def test_xmlattr_rejects_slash_in_key():
    try:
        attr_filter({"bad/key": "v"})
        raised = False
    except (ValueError, jinja2.exceptions.SecurityError):
        raised = True
    assert raised
'''

_REAL_MAIN = '''"""Fixture app code under remediation."""
import jinja2

_env = jinja2.Environment(autoescape=True)


def render_greeting(name):
    return _env.from_string("Hello {{ name }}").render(name=name)


def attr_filter(attrs):
    items = []
    for key, value in attrs.items():
        if " " in key or "/" in key:
            raise ValueError(f"invalid attribute key: {key!r}")
        items.append(f'{key}="{value}"')
    return " ".join(items)
'''


def _make_artifact(incident_id: str) -> str:
    """Artifact directory in the REAL persisted layout (app/ subdir)."""
    root = Path(tempfile.mkdtemp(prefix="ns-art-"))
    art = root / incident_id
    (art / "app").mkdir(parents=True)
    (art / "requirements-original.txt").write_text("jinja2==3.1.2\nMarkupSafe>=2.0\n")
    (art / "requirements-proposed.txt").write_text(
        f"{REMEDIATION_PACKAGE}==3.1.4\n" + "\n".join(sorted(SUPPORTING_PINS)) + "\n")
    (art / "requirements.diff").write_text("-jinja2==3.1.2\n+jinja2==3.1.4\n")
    for name in ("main.py", "pytest.ini"):
        (art / "app" / name).write_text("# snapshot\n")
    (art / "app" / "test_regression.py").write_text(_REAL_TESTS)
    (art / "app" / "main.py").write_text(_REAL_MAIN)
    (art / "manifest.json").write_text('{"files": {}, "digest": "abc"}')
    return str(art)


class TestProposalUsesExactArtifactBytes(unittest.TestCase):
    """Gap 2: committed requirements must be the artifact's exact bytes."""

    def setUp(self):
        self.incident_id = "repair-props-001"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def test_committed_requirements_match_artifact_bytes(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256x", {})
        self.assertNotIn("error", prop)
        repo = Path(prop["repo_dir"])
        committed = (repo / "requirements.txt").read_text()
        artifact_bytes = (Path(self.art) / "requirements-proposed.txt").read_text()
        self.assertEqual(committed, artifact_bytes,
                         "committed requirements must be the artifact's exact bytes")
        self.assertIn("MarkupSafe==", committed)  # strict pin, not >=2.0

    def test_committed_tree_contains_snapshot_app_files(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256x", {})
        self.assertNotIn("error", prop)
        repo = Path(prop["repo_dir"])
        r = gp._git(repo, "ls-files")
        tracked = set(r.stdout.split())
        for name in ("main.py", "test_regression.py", "pytest.ini"):
            self.assertIn(name, tracked,
                          f"QA-tested file {name} missing from committed tree")

    def test_missing_required_artifact_file_fails(self):
        # Remove a QA-tested file from the artifact snapshot.
        (Path(self.art) / "app" / "test_regression.py").unlink()
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256x", {})
        self.assertIn("error", prop)


class TestVerifyCheckoutIsGenuine(unittest.TestCase):
    """Gap 1: verification must run in an isolated env built from the
    committed requirements, measuring installed versions from it."""

    def setUp(self):
        self.incident_id = "repair-verif-002"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def test_verify_rejects_when_committed_tests_missing(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256v", {})
        self.assertNotIn("error", prop)
        # Simulate a committed tree without tests by deleting the file and
        # committing the deletion.
        repo = Path(prop["repo_dir"])
        (repo / "test_regression.py").unlink()
        gp._git(repo, "add", "-A")
        gp._git(repo, "commit", "-m", "remove tests")
        result = gp.verify_checkout(prop)
        self.assertFalse(result["verified"])
        self.assertTrue(result["error"] or result.get("collected_tests", 0) == 0)

    def test_verify_reports_mismatch_when_commit_moved(self):
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256v", {})
        self.assertNotIn("error", prop)
        tampered = dict(prop)
        tampered["proposal_commit"] = "0" * 40
        result = gp.verify_checkout(tampered)
        self.assertFalse(result["verified"])
        self.assertIn("commit", (result["error"] or "").lower())

    def test_verify_does_not_echo_proposed_version(self):
        """installed_version must come from the built environment, never be
        copied from proposed_version without measurement."""
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  "sha256v", {})
        self.assertNotIn("error", prop)
        # Break installation by pointing the proposal at a repo whose
        # requirements file was mutated post-commit (dirty tree).
        repo = Path(prop["repo_dir"])
        (repo / "requirements.txt").write_text("jinja2==3.1.4\n# tampered\n")
        result = gp.verify_checkout(prop)
        # Either verification fails outright, or installed_version is a
        # measured value — it must never silently equal proposed_version on
        # a dirty/failed run with verified=True.
        if result["verified"]:
            self.assertNotEqual(result.get("installed_version_source"),
                                "proposed_version")


class TestApprovalBindsGit(unittest.TestCase):
    """Gap 4: approval must validate and record commit/tree."""

    def setUp(self):
        self.incident_id = "repair-appr-003"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def _verified_incident(self):
        from app.isolated_runner import _write_manifest
        manifest = _write_manifest(Path(self.art))
        prop = gp.create_proposal(self.incident_id, "3.1.4", self.art,
                                  manifest["digest"], manifest)
        self.assertNotIn("error", prop)
        ver = gp.verify_checkout(prop)
        return prop, {
            "id": self.incident_id,
            "status": "tested_patch_awaiting_human_approval",
            "events": [],
            "git_proposal": {**prop,
                             "verification": ver},
            "attempts": [{"qa": {"approval": True},
                          "engineer": {"proposal": {"package": "jinja2",
                                                    "version": "3.1.4"}},
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
                                        "total": 4, "failures": 0,
                                        "errors": 0, "skipped": 0},
                              "required_tests_ok": True,
                              "success": True,
                              "installer_invoked": True,
                              "requirements_validated": True,
                              "test_output": "4 passed"}}],
            "verified_patch": {
                "package": "jinja2", "version": "3.1.4",
                "proposal": {"package": "jinja2", "version": "3.1.4"},
                "artifact_sha256": manifest["digest"],
                "artifact_dir": self.art, "manifest": manifest,
            },
        }

    def test_approval_record_contains_commit_and_tree(self):
        from app.approval import approve
        prop, incident = self._verified_incident()
        record = approve(incident)
        self.assertTrue(record.get("approved"),
                        f"approval failed: {record.get('error')}")
        self.assertEqual(record.get("git", {}).get("commit"),
                         prop["proposal_commit"])
        self.assertEqual(record.get("git", {}).get("tree"),
                         prop["proposal_tree"])

    def test_approval_blocked_when_git_verification_failed(self):
        from app.approval import approve
        prop, incident = self._verified_incident()
        incident["git_proposal"]["verification"]["verified"] = False
        record = approve(incident)
        self.assertFalse(record.get("approved"))


class TestStagingComparesApprovalRecord(unittest.TestCase):
    """Gap 4: staging must compare Git state against the approval record."""

    def setUp(self):
        self.incident_id = "repair-stag-004"
        self.art = _make_artifact(self.incident_id)

    def tearDown(self):
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(Path(self.art).parent, ignore_errors=True)

    def _approved_incident(self):
        from app.approval import approve
        prop, incident = TestApprovalBindsGit._verified_incident(self)
        record = approve(incident)
        self.assertTrue(record.get("approved"))
        return prop, incident

    def test_release_rejected_when_proposal_descriptor_lies(self):
        """A mutated/moved checkout must fail the gate even if the mutable
        git_proposal descriptor is doctored to claim the old commit."""
        from app.staging import _release_gate
        prop, incident = self._approved_incident()
        # Move the checkout to a new commit (simulating drift) and doctor
        # the descriptor to repeat the approved values.
        repo = Path(prop["repo_dir"])
        (repo / "requirements.txt").write_text(
            "jinja2==3.1.4\n# drifted\n" + "MarkupSafe==2.1.5\n")
        gp._git(repo, "add", "-A")
        gp._git(repo, "commit", "-m", "drift")
        incident["git_proposal"] = {
            **incident["git_proposal"],
            "proposal_commit": prop["proposal_commit"],
            "proposal_tree": prop["proposal_tree"],
        }
        reason = _release_gate(incident)
        self.assertIsNotNone(reason)
        self.assertIn("git", reason.lower())


class TestWorkflowBlocksOnGitFailure(unittest.TestCase):
    """Gap 3: failed Git verification must block approval state."""

    def test_git_failure_does_not_reach_awaiting_approval(self):
        from app import workflow as wf

        def fake_sentry(log_key):
            return {"disposition": "ACTIONABLE", "reason": "eval", "raw": ""}

        def fake_engineer(context, previous_rejection=None):
            return {"proposal": {"package": "jinja2", "version": "3.1.4"},
                    "fault_injection": False,
                    "investigation": {},
                    "reason": "ok", "raw": "", "model": "mock",
                    "elapsed_ms": 0.0, "error": None}

        def fake_runner(artifact):
            return {"artifact_sha256": artifact.get("sha256"),
                    "artifact_dir": artifact.get("artifact_dir"),
                    "manifest": artifact.get("manifest"),
                    "install_ok": True,
                    "version_inspection_ok": True,
                    "installed_version": "3.1.4",
                    "pytest_returncode": 0,
                    "junit": {"tests": {
                        "test_render_greeting_escapes_html": "passed",
                        "test_xmlattr_renders_normal_attributes": "passed",
                        "test_xmlattr_rejects_space_in_key": "passed",
                        "test_xmlattr_rejects_slash_in_key": "passed"},
                        "total": 4, "failures": 0, "errors": 0, "skipped": 0},
                    "required_tests_ok": True, "success": True, "error": None,
                    "test_output": "4 passed", "requirements_validated": True}

        def fake_qa(ver):
            return {"approval": True, "reason": "ok"}

        def fake_persist(incident_id, version, fault_injection=False,
                         base_dir=None):
            d = Path(tempfile.mkdtemp(prefix="ns-wf-"))
            (d / "requirements-original.txt").write_text("jinja2==3.1.2\n")
            (d / "requirements-proposed.txt").write_text(
                "jinja2==3.1.4\nMarkupSafe==2.1.5\n")
            (d / "requirements.diff").write_text("-a\n+b\n")
            (d / "app").mkdir()
            for f in ("main.py", "test_regression.py", "pytest.ini"):
                (d / "app" / f).write_text("# x\n")
            return {"package": "jinja2", "proposed_version": version,
                    "fault_injection": False, "artifact_dir": str(d),
                    "original_requirements": "", "proposed_requirements": "",
                    "diff": "-a\n+b\n",
                    "manifest": {"files": {}, "digest": "d"},
                    "sha256": "d"}

        def fake_create(incident_id, version, artifact_dir, sha, manifest):
            return {"error": "simulated git failure"}

        with patch("app.workflow.persist_artifact", fake_persist), \
             patch("app.git_proposal.create_proposal", fake_create):
            incident = wf.run_incident(
                log_key="actionable", fault_inject_first=False,
                engineer_fn=fake_engineer, sentry_fn=fake_sentry,
                runner_fn=fake_runner, qa_fn=fake_qa)

        # With Git delivery part of the workflow and its creation failed,
        # the incident must NOT be in the approval-ready state.
        self.assertNotEqual(incident.get("status"),
                            "tested_patch_awaiting_human_approval")
        self.assertEqual(incident.get("status"), "remediation_failed")
        self.assertIn("git", (incident.get("reason") or "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
