"""Mock tests for the mixed synthetic incident inbox.

Tests cover classification policy, signal separation, and remediation
gating — all with injected triage/workflow functions, no model calls.
"""
import unittest
from unittest.mock import MagicMock

from app import inbox as ib
from app import sentry_agent


def make_triage(disposition, log_key=None, **overrides):
    """Return a triage function that yields the given disposition."""
    def triage_fn(key):
        base = {
            "log_key": key,
            "log": sentry_agent.SYNTHETIC_FIXTURES.get(key, ""),
            "disposition": disposition,
            "model_disposition": disposition,
            "model_assessed_severity": "INFO",
            "operational_priority": "LOW",
            "reason": f"mock {disposition}",
            "advisory_severity": "MODERATE",
            "advisory_metadata": {},
            "synthetic": True,
            "scenario_label": None,
            "policy_override": None,
            "elapsed_ms": 1.0,
            "utc_timestamp": "2026-11-01T00:00:00+00:00",
            "error": None,
        }
        base.update(overrides)
        return base
    return triage_fn


class TestInboxClassification(unittest.TestCase):
    """Each inbox signal must be classified; the batch must contain all 5."""

    def test_inbox_batch_has_five_signals(self):
        self.assertEqual(len(ib.INBOX_BATCH), 5)

    def test_inbox_batch_keys_match_fixtures(self):
        for item in ib.INBOX_BATCH:
            self.assertIn(item["key"], sentry_agent.SYNTHETIC_FIXTURES)

    def test_expected_labels_stored_not_in_fixture_text(self):
        """Expected labels are evaluation metadata in INBOX_BATCH, never
        in the log text sent to the model."""
        for item in ib.INBOX_BATCH:
            self.assertIn("expected_label", item)
            log = sentry_agent.SYNTHETIC_FIXTURES[item["key"]]
            self.assertNotIn(item["expected_label"], log,
                             f"expected label leaked into log for {item['key']}")


class TestSignalSeparation(unittest.TestCase):
    """Different dispositions must be recorded in the right buckets."""

    def _run(self, dispositions):
        """Map each inbox key to a disposition in order."""
        it = iter(dispositions)

        def triage_fn(key):
            d = next(it)
            return make_triage(d)(key)

        def noop_workflow(**kw):
            return {"id": kw.get("incident_id", "x"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        return ib.process_inbox(triage_fn=triage_fn,
                                workflow_fn=noop_workflow,
                                fault_inject_first=False)

    def test_benign_signals_not_actionable(self):
        out = self._run(["BENIGN"] * 5)
        self.assertEqual(out["summary"]["benign"], 5)
        self.assertEqual(out["summary"]["actionable"], 0)
        self.assertEqual(out["summary"]["needs_human_review"], 0)
        self.assertIsNone(out["recovery_incident"])

    def test_needs_human_review_signals_become_handoffs(self):
        out = self._run(["NEEDS_HUMAN_REVIEW"] * 5)
        self.assertEqual(out["summary"]["needs_human_review"], 5)
        self.assertEqual(len(out["handoffs"]), 5)
        for h in out["handoffs"]:
            self.assertEqual(h["status"], "needs_human_review")
            self.assertEqual(h["attempts"], [])
            self.assertIsNone(h["elapsed_alert_to_tested_patch_ms"])

    def test_mixed_dispositions_separated_correctly(self):
        out = self._run([
            "BENIGN",            # inbox_success
            "BENIGN",            # inbox_app_error
            "ACTIONABLE",        # inbox_dep_scan
            "BENIGN",            # inbox_app_warning
            "NEEDS_HUMAN_REVIEW",  # inbox_incomplete
        ])
        self.assertEqual(out["summary"]["benign"], 3)
        self.assertEqual(out["summary"]["actionable"], 1)
        self.assertEqual(out["summary"]["needs_human_review"], 1)
        self.assertEqual(len(out["handoffs"]), 1)
        self.assertIsNotNone(out["recovery_incident"])


class TestRemediationGating(unittest.TestCase):
    """Only one actionable signal may start the recovery workflow."""

    def test_first_actionable_starts_recovery(self):
        called_with = {}

        def triage_fn(key):
            return make_triage("ACTIONABLE")(key)

        def tracking_workflow(**kw):
            called_with.update(kw)
            return {"id": kw.get("incident_id"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=tracking_workflow,
                               fault_inject_first=True)
        self.assertTrue(out["summary"]["recovery_started"])
        self.assertTrue(called_with.get("fault_inject_first"))

    def test_second_actionable_does_not_start_second_workflow(self):
        """With all 5 signals ACTIONABLE, only ONE workflow is started."""
        call_count = 0

        def triage_fn(key):
            return make_triage("ACTIONABLE")(key)

        def counting_workflow(**kw):
            nonlocal call_count
            call_count += 1
            return {"id": kw.get("incident_id"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=counting_workflow,
                               fault_inject_first=False)
        self.assertEqual(call_count, 1)
        self.assertEqual(out["summary"]["actionable"], 5)

    def test_uncertain_signal_does_not_authorize_remediation(self):
        """A NEEDS_HUMAN_REVIEW signal must remain a separate handoff
        and never start the recovery workflow."""
        def triage_fn(key):
            return make_triage("NEEDS_HUMAN_REVIEW")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(len(out["handoffs"]), 5)
        for h in out["handoffs"]:
            self.assertEqual(h["attempts"], [])
            self.assertIsNone(h["elapsed_alert_to_tested_patch_ms"])

    def test_handoff_preserves_original_reason_and_override(self):
        def triage_fn(key):
            return make_triage("NEEDS_HUMAN_REVIEW",
                               reason="insufficient data",
                               policy_override="Insufficient evidence")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        for h in out["handoffs"]:
            self.assertEqual(h["reason"], "insufficient data")
            self.assertIn("Insufficient", h["policy_override"])

    def test_inbox_results_preserve_advisory_connection(self):
        """Each result must carry advisory metadata linking it to
        repository findings (or an empty dict for non-advisory signals)."""
        def triage_fn(key):
            return make_triage("BENIGN",
                               advisory_metadata={"CVE-2024-34064": {"source": "ghsa"}})(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        for r in out["inbox_results"]:
            self.assertIn("advisory_metadata", r)
            self.assertIn("advisory_severity", r)


class TestNoAttackOrExploitabilityClaims(unittest.TestCase):
    """Inbox output must not claim an attack occurred or that logs prove
    exploitability."""

    def test_no_attack_claims_in_results(self):
        def triage_fn(key):
            return make_triage("ACTIONABLE")(key)

        def noop_workflow(**kw):
            return {"id": "x", "status": "blocked", "events": [],
                    "attempts": [], "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=noop_workflow,
                               fault_inject_first=False)
        import json
        text = json.dumps(out).lower()
        for word in ("attack occurred", "proves exploitability",
                     "confirmed exploit"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
