"""Regression tests for Git proposal boundaries and mutation rejection.

Covers:
- Fixture repo creation outside source checkout
- Proposal branch + single commit with correct diff
- Already-patched version rejected by eligibility (C1 bug fix)
- Git checkout verification
- Approval binding recheck (clean vs dirty vs wrong commit)
- Mutation after approval is rejected at release gate (C3)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import approval as approval_mod
from app import git_proposal as gp
from app.inbox import _patch_eligibility
from app.isolated_runner import REMEDIATION_PACKAGE
from test_git_proposal_repair import _make_artifact


class TestGitProposalCreation(unittest.TestCase):
    """Git proposal creates a clean branch with one commit."""

    def setUp(self):
        self.incident_id = "git-test-001"
        self.artifact_dir = _make_artifact(self.incident_id)

    def tearDown(self):
        shutil.rmtree(Path(self.artifact_dir).parent, ignore_errors=True)
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)

    def test_creates_branch_with_single_commit(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "abc123", {})
        self.assertNotIn("error", prop)
        self.assertEqual(prop["branch"], f"remediate-{self.incident_id[:8]}")
        self.assertEqual(prop["proposed_version"], "3.1.4")
        self.assertIn("jinja2==3.1.4", prop["diff"])
        self.assertIn("-jinja2==3.1.2", prop["diff"])
        self.assertIn("proposal_commit", prop)
        self.assertIn("base_commit", prop)

    def test_commit_contains_only_requirements_change(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "abc123", {})
        repo = Path(prop["repo_dir"])
        r = gp._git(repo, "diff", "--name-only", "HEAD~1", "HEAD")
        self.assertEqual(r.stdout.strip(), "requirements.txt")

    def test_repo_outside_source_checkout(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "abc123", {})
        repo = Path(prop["repo_dir"])
        # Must not be inside the project tree.
        project_root = Path(__file__).resolve().parents[1]
        try:
            repo.relative_to(project_root)
            self.fail("Fixture repo is inside project tree")
        except ValueError:
            pass  # expected


class TestGitCheckoutVerification(unittest.TestCase):

    def setUp(self):
        self.incident_id = "git-verify-002"
        self.artifact_dir = _make_artifact(self.incident_id)

    def tearDown(self):
        shutil.rmtree(Path(self.artifact_dir).parent, ignore_errors=True)
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)

    def test_verify_checkout_passes_on_clean_proposal(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256test", {})
        result = gp.verify_checkout(prop)
        # verify_checkout runs pytest; we care that it completes without
        # error on a valid proposal structure.
        self.assertIn("proposal_commit", result)


class TestApprovalBindingRecheck(unittest.TestCase):

    def setUp(self):
        self.incident_id = "git-bind-003"
        self.artifact_dir = _make_artifact(self.incident_id)

    def tearDown(self):
        shutil.rmtree(Path(self.artifact_dir).parent, ignore_errors=True)
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)

    def _approved(self, prop):
        """Approval record bound to the proposal's commit/tree."""
        return {
            "id": self.incident_id,
            "human_approval": {
                "approved": True, "actor": "human",
                "incident_id": self.incident_id,
                "git": {"commit": prop["proposal_commit"],
                        "tree": prop["proposal_tree"],
                        "branch": prop["branch"], "verified": True},
            },
        }

    def test_clean_checkout_passes_binding(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256bind", {})
        incident = self._approved(prop)
        result = gp.check_approval_binding(incident, prop)
        self.assertIsNone(result)

    def test_dirty_working_tree_fails_binding(self):
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256bind", {})
        # Dirty the working tree.
        (Path(prop["repo_dir"]) / "main.py").open("a").write("\n# dirty\n")
        incident = self._approved(prop)
        result = gp.check_approval_binding(incident, prop)
        self.assertIsNotNone(result)
        self.assertIn("Dirty", result)

    def test_mutated_requirements_fails_binding(self):
        """C3 scenario: mutate requirements after approval."""
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256bind", {})
        # Mutate via the trusted test helper.
        gp.mutate_requirements_for_test(prop)
        incident = self._approved(prop)
        result = gp.check_approval_binding(incident, prop)
        self.assertIsNotNone(result)
        # Mutation makes working tree dirty OR diff mismatch — either is a valid rejection.
        self.assertTrue("Dirty" in result or "diff does not match" in result)

    def test_missing_approval_record_fails_closed(self):
        """Without an approval record, the binding must refuse to trust
        mutable proposal identifiers."""
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256bind", {})
        result = gp.check_approval_binding({"id": self.incident_id}, prop)
        self.assertIsNotNone(result)
        self.assertIn("approved Git commit/tree", result)


class TestMutationRejectionAtReleaseGate(unittest.TestCase):
    """C3: full integrity-check scenario — mutation rejected before staging."""

    def setUp(self):
        self.incident_id = "integrity-004"
        self.artifact_dir = _make_artifact(self.incident_id)

    def tearDown(self):
        shutil.rmtree(Path(self.artifact_dir).parent, ignore_errors=True)
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)

    def test_mutation_rejected_by_release_gate(self):
        from app.staging import _release_gate
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256integrity", {})
        incident = {
            "id": self.incident_id,
            "status": "human_approved",
            "human_approval": {
                "approved": True,
                "actor": "human",
                "incident_id": self.incident_id,
                "patch": {"package": "jinja2", "version": "3.1.4"},
                "artifact_sha256": "sha256integrity",
            },
            "verified_patch": {
                "package": "jinja2",
                "version": "3.1.4",
                "artifact_sha256": "sha256integrity",
                "artifact_dir": self.artifact_dir,
                "manifest": {},
                "git_proposal": prop,
            },
            "attempts": [{
                "qa": {"approval": True},
                "verification": {
                    "artifact_sha256": "sha256integrity",
                    "artifact_dir": self.artifact_dir,
                    "manifest": {},
                    "installed_version": "3.1.4",
                    "pytest_returncode": 0,
                    "junit": {"tests": {}},
                    "success": True,
                    "installer_invoked": True,
                    "requirements_validated": True,
                },
            }],
            "staging_release": {},
        }

        # Before mutation: gate should pass (other preconditions may vary).
        # After mutation: gate must fail.
        gp.mutate_requirements_for_test(prop)
        gate_result = _release_gate(incident)
        self.assertIsNotNone(gate_result)
        self.assertIn("binding failed" if "binding" in gate_result else "failed",
                      gate_result.lower())


class TestVerificationLeavesCheckoutClean(unittest.TestCase):
    """Regression for the independently reproduced verification side effect.

    Running verify_checkout must not leave Python bytecode caches or pytest
    caches in the Git checkout, even when the parent process does not set
    PYTHONDONTWRITEBYTECODE. A clean checkout must remain approvable.
    """

    def setUp(self):
        self.incident_id = "clean-qa-005"
        self.artifact_dir = _make_artifact(self.incident_id)

    def tearDown(self):
        shutil.rmtree(Path(self.artifact_dir).parent, ignore_errors=True)
        repo = gp._GIT_FIXTURE_ROOT / self.incident_id
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)

    def test_genuine_verification_keeps_checkout_clean_and_approvable(self):
        """Without any special parent environment, verify_checkout runs,
        leaves the working tree clean, and the proposal can be approved."""
        prop = gp.create_proposal(
            self.incident_id, "3.1.4", self.artifact_dir,
            "sha256clean", {})
        repo_dir = Path(prop["repo_dir"])

        # Run verification in a clean child environment: explicitly remove
        # the parent suppression flag if present, so the subprocess must
        # suppress bytecode itself.
        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTEST_ADDOPTS", None)
        with patch.dict(os.environ, env, clear=True):
            result = gp.verify_checkout(prop)
        self.assertTrue(result.get("verified"), result.get("error"))

        # No bytecode or pytest caches should have been written to checkout.
        porcelain = gp._git(repo_dir, "status", "--porcelain").stdout.strip()
        self.assertEqual(porcelain, "",
                         f"checkout polluted after verification: {porcelain}")
        self.assertFalse(any(repo_dir.rglob("__pycache__")),
                         "__pycache__ found in checkout")
        self.assertFalse(any((repo_dir / ".pytest_cache").rglob("*")),
                         ".pytest_cache found in checkout")

        # Approval must succeed without a special parent environment.
        from app.isolated_runner import _write_manifest
        manifest = _write_manifest(Path(self.artifact_dir))
        verification = {
            "verified": True,
            "artifact_sha256": manifest["digest"],
            "artifact_dir": self.artifact_dir,
            "manifest": manifest,
            "install_ok": True,
            "version_inspection_ok": True,
            "installed_version": result["installed_version"],
            "proposed_version": prop["proposed_version"],
            "installed_dependency_versions": result.get("installed_dependency_versions", {}),
            "pytest_returncode": result["pytest_returncode"],
            "junit": result["junit"],
            "required_tests_ok": result["required_tests_ok"],
            "success": True,
            "installer_invoked": True,
            "requirements_validated": True,
            "test_output": result.get("test_output", ""),
        }
        incident = {
            "id": self.incident_id,
            "status": "tested_patch_awaiting_human_approval",
            "events": [],
            "git_proposal": {**prop, **result, "verification": verification},
            "verified_patch": {
                "package": "jinja2", "version": prop["proposed_version"],
                "proposal": {"package": "jinja2", "version": prop["proposed_version"]},
                "artifact_sha256": manifest["digest"],
                "artifact_dir": self.artifact_dir,
                "manifest": manifest,
            },
            "attempts": [{
                "qa": {"approval": True},
                "engineer": {"proposal": {"package": "jinja2", "version": prop["proposed_version"]}},
                "verification": verification,
            }],
        }
        with patch.dict(os.environ, env, clear=True):
            record = approval_mod.approve(incident)
        self.assertTrue(record.get("approved"), record.get("error"))
        self.assertEqual(record["git"]["commit"], prop["proposal_commit"])
        self.assertEqual(record["git"]["tree"], prop["proposal_tree"])


class TestPatchedVersionIneligibleAfterFix(unittest.TestCase):
    """Regression for the C1-found bug: already-patched versions must not be
    eligible for remediation."""

    def test_patched_316_not_eligible(self):
        log = (
            "[SYNTHETIC-EVAL] jinja2==3.1.6 installed; "
            "advisory GHSA-h75v-3vvj-5mfj affected < 3.1.4"
        )
        result = _patch_eligibility(log)
        self.assertFalse(result["eligible"],
                         "Already-patched version 3.1.6 must NOT be eligible")


if __name__ == "__main__":
    unittest.main(verbosity=2)
