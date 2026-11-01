"""Bounded mutation-rejection scenario (C3, repaired).

1. Produce a genuinely QA-verified incident (real Git proposal + isolated
   checkout verification, mocked model/runner layer as in the test suite).
2. Record explicit approval through app.approval.approve (which binds the
   Git commit/tree into the approval record).
3. Mutate ONLY the Git checkout requirements file (uncommitted change).
4. Call the real app.staging.release.
5. Assert rejection specifically identifies the Git binding change, and
   that environment creation (_make_env) and process start (_start) were
   never invoked.

Evidence is preserved privately before/after.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import git_proposal as gp
from app import workflow as wf
from app import approval as approval_mod
from app import staging as staging_mod
from app.isolated_runner import REMEDIATION_PACKAGE, SUPPORTING_PINS

PRIVATE = Path(os.environ.get(
    "NS_PRIVATE_DIR",
    os.path.expanduser("~/night-shift-mel-private/evaluation")))


def _persist_artifact(incident_id: str) -> dict:
    """Persist a real artifact snapshot (trusted fixture files, real manifest)."""
    from app.isolated_runner import persist_artifact
    artifact = persist_artifact(incident_id, "3.1.4", fault_injection=False)
    assert artifact is not None, "artifact persistence rejected 3.1.4"
    return artifact


class MutationRejectionScenario(unittest.TestCase):

    def test_mutation_rejected_by_staging_release_before_env(self):
        incident_id = "integrity-mut-002"

        # --- 1. Genuinely verified incident (workflow layer mocked for the
        # model/runner; Git proposal + checkout verification are REAL). ---
        artifact = _persist_artifact(incident_id)

        def fake_sentry(log_key):
            return {"disposition": "ACTIONABLE", "reason": "eval", "raw": ""}

        def fake_engineer(context, previous_rejection=None):
            return {"proposal": {"package": REMEDIATION_PACKAGE,
                                 "version": "3.1.4"},
                    "fault_injection": False, "investigation": {},
                    "reason": "ok", "raw": "", "model": "mock",
                    "elapsed_ms": 0.0, "error": None}

        def fake_runner(art):
            # Real isolated verification of the artifact snapshot.
            from app.isolated_runner import verify_patch
            return verify_patch(art)

        incident = wf.run_incident(
            log_key="actionable", fault_inject_first=False,
            incident_id=incident_id,
            engineer_fn=fake_engineer, sentry_fn=fake_sentry,
            runner_fn=fake_runner, qa_fn=lambda v: {"approval": True, "reason": "ok"})

        self.assertEqual(incident["status"],
                         "tested_patch_awaiting_human_approval",
                         f"workflow status: {incident.get('status')} — "
                         f"{incident.get('reason')}")
        git_prop = incident["git_proposal"]
        self.assertTrue(git_prop.get("verified"),
                        f"git verification: {git_prop.get('error')}")

        # --- 2. Explicit approval binds Git commit/tree. ---
        record = approval_mod.approve(incident)
        self.assertTrue(record.get("approved"),
                        f"approval failed: {record.get('error')}")
        self.assertEqual(record["git"]["commit"], git_prop["proposal_commit"])
        self.assertEqual(record["git"]["tree"], git_prop["proposal_tree"])

        # --- Prove the UNCHANGED approved incident passes the gate. ---
        gate_before = staging_mod._release_gate(incident)
        self.assertIsNone(gate_before,
                          f"clean approved incident must pass the gate: {gate_before}")

        # --- 3. Mutate ONLY the Git checkout requirements. ---
        mutated = gp.mutate_requirements_for_test(git_prop)
        self.assertIn("9.9.9", mutated)

        # --- 4/5. Call the real staging.release; assert Git-specific
        # rejection before environment creation or process start. ---
        env_calls = []
        start_calls = []
        with patch.object(staging_mod, "_make_env",
                          side_effect=lambda *a, **k: env_calls.append(1) or None), \
             patch.object(staging_mod, "_start",
                          side_effect=lambda *a, **k: start_calls.append(1) or None):
            result = staging_mod.release(incident)

        self.assertFalse(result.get("released"),
                         "staging.release must reject a mutated checkout")
        err = (result.get("error") or "").lower()
        self.assertIn("git", err)
        # An uncommitted mutation surfaces as a dirty working tree; a
        # committed change would surface as "changed after approval".
        self.assertTrue(
            "dirty working tree" in err or "changed after approval" in err,
            f"rejection must identify the Git content/binding change: {err}")
        self.assertEqual(env_calls, [], "_make_env must never be called")
        self.assertEqual(start_calls, [], "_start must never be called")

        # --- Preserve before/after evidence privately. ---
        PRIVATE.mkdir(parents=True, exist_ok=True)
        (PRIVATE / "mutation-rejection-report.json").write_text(json.dumps({
            "scenario": "mutation_rejection_c3_repaired",
            "incident_id": incident_id,
            "approved": True,
            "approval_git": record.get("git"),
            "gate_before_mutation": gate_before,
            "mutation_applied": mutated.strip(),
            "release_rejected": True,
            "rejection_reason": result.get("error"),
            "env_creation_invoked": bool(env_calls),
            "process_start_invoked": bool(start_calls),
            "artifact_sha256": artifact.get("sha256"),
        }, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
