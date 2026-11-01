"""Focused mock tests for the grounded interpretation protocol and live progress.

No real model calls. The engineer agent's single interpretation call is
replaced with injectable mocks; evidence collection is deterministic and
exercised for real against the fixture. The grounding gate, citation
verification, evidence-read failures, malformed output, bounded retry,
needs_human_review handoff, and workflow blocking are exercised for real.

Live-progress mock tests use the /workflow-sync endpoint and the
/workflow/status polling endpoint with the in-memory store.
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import eng_tools, engineer_agent, workflow


REQUIRED_TESTS = {
    "test_render_greeting_escapes_html",
    "test_xmlattr_renders_normal_attributes",
    "test_xmlattr_rejects_space_in_key",
    "test_xmlattr_rejects_slash_in_key",
}


def make_interpretation_response():
    """A model interpretation that passes the grounding gate."""
    return {
        "declared_version": "3.1.2",
        "applicability": "The application renders xmlattr on user-supplied keys, so the advisory applies.",
        "uncertainty": "Exploitability depends on runtime inputs, which the fixture does not show.",
        "evidence": [
            {"file": "main.py", "line": 10,
             "finding": "render_attrs renders user keys through xmlattr"},
            {"file": "requirements-affected.txt", "line": 4,
             "finding": "declares jinja2==3.1.2"},
        ],
        "proposal": {"package": "jinja2", "version": "3.1.4"},
        "rationale": "3.1.4 patches both CVEs per the advisory metadata.",
    }


def fake_model_fn(response_dict):
    """Create a model_fn that always returns the given response."""
    def _fn(system_prompt, user_prompt, expect_json=True, timeout=120.0):
        return {
            "success": True,
            "response": response_dict,
            "raw": json.dumps(response_dict),
            "elapsed_ms": 10.0,
            "error": None,
        }
    return _fn


def fake_model_error():
    def _fn(system_prompt, user_prompt, expect_json=True, timeout=120.0):
        return {"success": False, "response": None, "raw": "",
                "elapsed_ms": 5.0, "error": "connection refused"}
    return _fn


def make_scripted_model_fn(script):
    """Feed a fixed sequence of responses, cycling if the script is exhausted."""
    idx = {"i": 0}
    def _fn(system_prompt, user_prompt, expect_json=True, timeout=120.0):
        r = script[idx["i"] % len(script)]
        idx["i"] += 1
        return {"success": True, "response": r, "raw": json.dumps(r),
                "elapsed_ms": 10.0, "error": None}
    return _fn


def make_malformed_model_fn():
    """Model returns a JSON string instead of an object."""
    def _fn(system_prompt, user_prompt, expect_json=True, timeout=120.0):
        return {"success": True, "response": "not a dict",
                "raw": '"not a dict"', "elapsed_ms": 10.0, "error": None}
    return _fn


# --------------------------------------------------------------------------
# eng_tools tests
# --------------------------------------------------------------------------

class TestEngTools(unittest.TestCase):

    def test_list_files_returns_fixture_files(self):
        result = eng_tools.list_files()
        self.assertIn("main.py", result["files"])
        self.assertIn("requirements-affected.txt", result["files"])

    def test_list_files_rejects_directory_arg(self):
        result = eng_tools.list_files(directory="app")
        self.assertIn("error", result)
        self.assertIn("no subdirectories", result["error"])

    def test_read_file_succeeds_for_direct_child(self):
        result = eng_tools.read_file("main.py")
        self.assertEqual(result["file"], "main.py")
        self.assertIn("content", result)
        self.assertIn("render_greeting", result["content"])

    def test_read_file_rejects_path_separator(self):
        result = eng_tools.read_file("app/main.py")
        self.assertIn("error", result)
        self.assertIn("bare file name", result["error"])

    def test_read_file_rejects_parent_reference(self):
        result = eng_tools.read_file("../main.py")
        self.assertIn("error", result)

    def test_read_file_rejects_nonexistent(self):
        result = eng_tools.read_file("nonexistent.py")
        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    def test_read_file_rejects_empty(self):
        result = eng_tools.read_file("")
        self.assertIn("error", result)


# --------------------------------------------------------------------------
# Orchestrator-selected evidence collection
# --------------------------------------------------------------------------

class TestEvidenceCollection(unittest.TestCase):

    def test_collect_evidence_reads_both_targets(self):
        reads, problems = engineer_agent._collect_evidence()
        self.assertEqual(problems, [])
        self.assertEqual(len(reads), 2)
        names = [r["result"]["file"] for r in reads]
        self.assertIn("main.py", names)
        self.assertIn("requirements-affected.txt", names)
        for entry in reads:
            self.assertEqual(entry["tool"], "read_file")
            self.assertIn("numbered_content", entry)
            self.assertIn("selected_by", entry)
            self.assertIn("orchestrator", entry["selected_by"])
            # Numbered contents must match the real file contents.
            self.assertIn("content", entry["result"])
            lines = entry["result"]["content"].splitlines()
            self.assertEqual(len(entry["numbered_content"]), len(lines))
            self.assertEqual(entry["numbered_content"]["1"], lines[0])

    def test_collect_evidence_records_read_failure(self):
        """A failing evidence read is recorded and reported as a problem."""
        with patch.object(engineer_agent, "EVIDENCE_TARGETS",
                          (("nonexistent.py", "missing source"),)):
            reads, problems = engineer_agent._collect_evidence()
        self.assertEqual(len(problems), 1)
        self.assertIn("nonexistent.py", problems[0])
        self.assertIn("missing source", problems[0])
        self.assertEqual(len(reads), 1)
        self.assertNotIn("numbered_content", reads[0])
        self.assertIn("error", reads[0]["result"])

    def test_evidence_read_failure_fails_grounding_and_hands_off(self):
        """Evidence-read failure → grounding fails → needs_human_review,
        with no installer call."""
        with patch.object(engineer_agent, "EVIDENCE_TARGETS",
                          (("nonexistent.py", "missing source"),)):
            model_fn = fake_model_fn(make_interpretation_response())
            out = engineer_agent.propose_upgrade("ctx", None, model_fn=model_fn)
        self.assertIsNone(out["proposal"])
        inv = out["investigation"]
        self.assertTrue(inv["needs_human_review"])
        self.assertTrue(any("failed" in p for p in inv["grounding_problems"]))


# --------------------------------------------------------------------------
# Grounding gate tests (engineer_agent) — interpretation protocol
# --------------------------------------------------------------------------

class TestGroundingGate(unittest.TestCase):

    def test_grounded_interpretation_passes(self):
        model_fn = fake_model_fn(make_interpretation_response())
        out = engineer_agent.propose_upgrade("ctx", None, model_fn=model_fn)
        self.assertIsNotNone(out["proposal"])
        self.assertEqual(out["proposal"]["version"], "3.1.4")
        inv = out["investigation"]
        self.assertFalse(inv["needs_human_review"])
        self.assertIsNone(inv["grounding_problems"])
        self.assertEqual(inv["citation_problems"], [])
        self.assertIn("main.py", inv["files_read"])
        self.assertIn("requirements-affected.txt", inv["files_read"])
        self.assertEqual(inv["declared_version"], "3.1.2")
        # Orchestrator-selected evidence collection is labeled as such.
        self.assertIn("orchestrator", inv["evidence_collection"]["selected_by"])
        self.assertEqual(inv["evidence_collection"]["problems"], [])
        self.assertIsNotNone(inv.get("applicability"))
        self.assertIsNotNone(inv.get("uncertainty"))

    def test_declared_vs_installed_versions_distinct(self):
        """The engineer's declared_version is the pin from the declaration,
        not an installed version; the workflow keeps them distinct."""
        model_fn = fake_model_fn(make_interpretation_response())
        out = engineer_agent.propose_upgrade("ctx", None, model_fn=model_fn)
        inv = out["investigation"]
        self.assertEqual(inv["declared_version"], "3.1.2")
        self.assertEqual(inv["fixture_declared_version"], "3.1.2")
        # The proposal version is the remediation target, distinct from the
        # declared version.
        self.assertEqual(out["proposal"]["version"], "3.1.4")
        self.assertNotEqual(out["proposal"]["version"], inv["declared_version"])

    def test_fabricated_citation_fails_verification(self):
        """A citation for a file that was never read is rejected."""
        resp = make_interpretation_response()
        resp["evidence"] = [
            {"file": "test_regression.py", "line": 5,
             "finding": "cited but never collected"},
        ]
        out = engineer_agent.propose_upgrade("ctx", None,
                                             model_fn=fake_model_fn(resp))
        self.assertIsNone(out["proposal"])
        inv = out["investigation"]
        self.assertTrue(inv["needs_human_review"])
        self.assertTrue(any("was not read" in p for p in inv["citation_problems"]))
        self.assertTrue(any("citations were verified" in p
                            for p in inv["grounding_problems"]))

    def test_out_of_range_citation_fails_verification(self):
        """A line number beyond the file's real length is rejected."""
        resp = make_interpretation_response()
        resp["evidence"] = [
            {"file": "main.py", "line": 999, "finding": "beyond EOF"},
        ]
        out = engineer_agent.propose_upgrade("ctx", None,
                                             model_fn=fake_model_fn(resp))
        inv = out["investigation"]
        self.assertTrue(any("out of range" in p for p in inv["citation_problems"]))

    def test_missing_source_evidence_fails_grounding(self):
        """All evidence from the declaration only (no application source
        citation) fails the gate."""
        resp = make_interpretation_response()
        resp["evidence"] = [
            {"file": "requirements-affected.txt", "line": 4,
             "finding": "declares jinja2==3.1.2"},
        ]
        out = engineer_agent.propose_upgrade("ctx", None,
                                             model_fn=fake_model_fn(resp))
        self.assertIsNone(out["proposal"])
        self.assertTrue(out["investigation"]["needs_human_review"])
        problems = out["investigation"]["grounding_problems"]
        self.assertTrue(any("application source" in p for p in problems))

    def test_empty_evidence_fails_grounding(self):
        resp = make_interpretation_response()
        resp["evidence"] = []
        out = engineer_agent.propose_upgrade("ctx", None,
                                             model_fn=fake_model_fn(resp))
        self.assertIsNone(out["proposal"])
        self.assertTrue(out["investigation"]["needs_human_review"])

    def test_wrong_declared_version_fails_grounding(self):
        resp = make_interpretation_response()
        resp["declared_version"] = "3.1.4"  # wrong — fixture says 3.1.2
        out = engineer_agent.propose_upgrade("ctx", None,
                                             model_fn=fake_model_fn(resp))
        self.assertIsNone(out["proposal"])
        self.assertTrue(out["investigation"]["needs_human_review"])
        problems = out["investigation"]["grounding_problems"]
        self.assertTrue(any("does not match" in p for p in problems))

    def test_malformed_model_output_hands_off(self):
        """A model response that is not a JSON object hands off honestly."""
        out = engineer_agent.propose_upgrade(
            "ctx", None, model_fn=make_malformed_model_fn())
        self.assertIsNone(out["proposal"])
        self.assertTrue(out["investigation"]["needs_human_review"])
        self.assertTrue(any("malformed" in p.lower()
                            for p in out["investigation"]["grounding_problems"]))

    def test_grounding_failure_blocks_installation(self):
        """needs_human_review from engineer stops the workflow before any
        installer call — verification stays None."""
        resp = make_interpretation_response()
        resp["evidence"] = []
        model_fn = fake_model_fn(resp)
        runner_fn = MagicMock()
        incident = workflow.run_incident(
            engineer_fn=lambda ctx, rej: engineer_agent.propose_upgrade(
                ctx, rej, model_fn=model_fn),
            runner_fn=runner_fn, fault_inject_first=True,
        )
        self.assertEqual(incident["status"], "needs_human_review")
        model_attempt = incident["attempts"][-1]
        self.assertIsNone(model_attempt["verification"])
        self.assertIsNone(model_attempt["qa"])
        self.assertEqual(runner_fn.call_count, 1)

    def test_bounded_retry_then_pass(self):
        """First attempt fails grounding, retry passes — proposal returned."""
        bad = make_interpretation_response()
        bad["evidence"] = []
        good = make_interpretation_response()
        model_fn = make_scripted_model_fn([bad, good])
        out = engineer_agent.propose_upgrade("ctx", None, model_fn=model_fn)
        self.assertIsNotNone(out["proposal"])
        self.assertEqual(out["proposal"]["version"], "3.1.4")
        self.assertFalse(out["investigation"]["needs_human_review"])
        self.assertTrue(out["investigation"].get("retry"))

    def test_model_error_produces_handoff(self):
        out = engineer_agent.propose_upgrade("ctx", None,
                                              model_fn=fake_model_error())
        self.assertIsNone(out["proposal"])
        self.assertTrue(out["investigation"]["needs_human_review"])

    def test_collected_reads_recorded_with_results(self):
        model_fn = fake_model_fn(make_interpretation_response())
        out = engineer_agent.propose_upgrade("ctx", None, model_fn=model_fn)
        inv = out["investigation"]
        self.assertEqual(len(inv["tool_calls"]), 2)
        for entry in inv["tool_calls"]:
            self.assertEqual(entry["tool"], "read_file")
            self.assertIn("args", entry)
            self.assertIn("result", entry)
            self.assertIn("numbered_content", entry)
        # The model prompt itself must have contained the numbered contents.
        self.assertIn("jinja2==3.1.2", out["raw"])


# --------------------------------------------------------------------------
# Live progress tests
# --------------------------------------------------------------------------

class TestLiveProgress(unittest.TestCase):
    """Test the /workflow background endpoint and /workflow/status polling
    using the FastAPI TestClient.

    The background workflow is replaced with a mocked run_incident so no
    real model calls or subprocesses are made. The polling, duplicate-guard,
    and status-not-found behaviors are still exercised for real.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        from app.main import _incidents, _running_workflows, _store_lock
        with _store_lock:
            _incidents.clear()
            _running_workflows.clear()

    def test_workflow_starts_background_and_polls(self):
        from app.main import _incidents, _store_lock

        mock_incident = {
            "id": "mock-inc-123",
            "status": "tested_patch_awaiting_human_approval",
            "log_key": "actionable",
            "events": [],
            "attempts": [],
            "elapsed_alert_to_tested_patch_ms": 12345.67,
        }
        with patch("app.main.run_incident", return_value=mock_incident):
            resp = self.client.post("/workflow")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "running")
            self.assertIn("id", data)
            inc_id = data["id"]

            st = self.client.get(f"/workflow/status/{inc_id}").json()
            self.assertEqual(st["incident_id"], inc_id)

            deadline = time.time() + 2.0
            while time.time() < deadline:
                st = self.client.get(f"/workflow/status/{inc_id}").json()
                if not st["running"]:
                    break
                time.sleep(0.02)

            self.assertFalse(st["running"])
            with _store_lock:
                self.assertIn(inc_id, _incidents)
                self.assertEqual(
                    _incidents[inc_id]["status"],
                    "tested_patch_awaiting_human_approval",
                )

    def test_duplicate_workflow_guard(self):
        from app.main import _running_workflows, _store_lock
        fake_id = "fake-running-id"
        with _store_lock:
            _running_workflows[fake_id] = True
        resp = self.client.post("/workflow")
        data = resp.json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["id"], fake_id)
        with _store_lock:
            _running_workflows.clear()

    def test_workflow_status_not_found(self):
        st = self.client.get("/workflow/status/nonexistent").json()
        self.assertIn("error", st)

    def test_workflow_sync_endpoint_exists(self):
        mock_incident = {
            "id": "mock-sync-456",
            "status": "tested_patch_awaiting_human_approval",
            "log_key": "actionable",
            "events": [],
            "attempts": [],
        }
        with patch("app.main.run_incident", return_value=mock_incident):
            resp = self.client.post("/workflow-sync")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["id"], "mock-sync-456")


if __name__ == "__main__":
    unittest.main(verbosity=2)
