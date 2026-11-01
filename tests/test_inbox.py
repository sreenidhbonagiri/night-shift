"""Mock tests for the mixed synthetic incident inbox.

Tests cover classification policy, signal separation, remediation
gating, and evidence-connection requirements — all with injected
triage/workflow functions, no model calls.
"""
import json
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

    def test_inbox_app_error_expected_label_is_benign(self):
        """The harmless application-error fixture's expected outcome must
        be BENIGN, not broadened."""
        item = next(i for i in ib.INBOX_BATCH if i["key"] == "inbox_app_error")
        self.assertEqual(item["expected_label"], "BENIGN")

    def test_inbox_app_warning_expected_label_is_investigate(self):
        """The application warning must be INVESTIGATE, not ACTIONABLE
        or BENIGN — it is not patch-eligible."""
        item = next(i for i in ib.INBOX_BATCH if i["key"] == "inbox_app_warning")
        self.assertEqual(item["expected_label"], "INVESTIGATE")


class TestSignalSeparation(unittest.TestCase):
    """Different dispositions must be recorded in the right buckets."""

    def _run(self, dispositions, advisory_metas=None):
        """Map each inbox key to a disposition in order."""
        it = iter(dispositions)
        mit = iter(advisory_metas or [{}] * 5)

        def triage_fn(key):
            d = next(it)
            m = next(mit)
            return make_triage(d, advisory_metadata=m)(key)

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
        """3 BENIGN + 1 ACTIONABLE (with dep evidence) + 1 NHR."""
        def triage_fn(key):
            if key == "inbox_dep_scan":
                return make_triage("ACTIONABLE",
                                   advisory_metadata={"CVE": {}})(key)
            if key == "inbox_incomplete":
                return make_triage("NEEDS_HUMAN_REVIEW")(key)
            return make_triage("BENIGN")(key)

        def noop_workflow(**kw):
            return {"id": kw.get("incident_id", "x"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=noop_workflow,
                               fault_inject_first=False)
        self.assertEqual(out["summary"]["benign"], 3)
        self.assertEqual(out["summary"]["actionable"], 1)
        self.assertEqual(out["summary"]["needs_human_review"], 1)
        self.assertEqual(len(out["handoffs"]), 1)
        # The ACTIONABLE signal has advisory metadata → remediation eligible
        self.assertEqual(out["summary"]["remediation_eligible"], 1)
        self.assertIsNotNone(out["recovery_incident"])


class TestRemediationGating(unittest.TestCase):
    """Only a signal with explicit dependency evidence may start the
    Jinja2 remediation workflow. Generic errors and warnings are blocked."""

    def test_actionable_with_dep_evidence_starts_recovery(self):
        """An ACTIONABLE signal WITH dependency evidence starts recovery."""
        called_with = {}

        def triage_fn(key):
            m = {"CVE-2024-34064": {}} if key == "inbox_dep_scan" else {}
            return make_triage("ACTIONABLE", advisory_metadata=m)(key)

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

    def test_generic_error_actionable_cannot_trigger_jinja2_remediation(self):
        """REGRESSION: an ACTIONABLE application error WITHOUT dependency
        evidence must NOT start the Jinja2 remediation workflow.
        Only inbox_app_error is ACTIONABLE; all others are BENIGN so
        only the app error is tested."""
        def triage_fn(key):
            if key == "inbox_app_error":
                return make_triage("ACTIONABLE", advisory_metadata={})(key)
            return make_triage("BENIGN", advisory_metadata={})(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(out["summary"]["remediation_eligible"], 0)
        # The actionable signal should be in investigate_signals, not
        # starting a workflow.
        self.assertGreater(len(out["investigate_signals"]), 0)

    def test_unrelated_warning_cannot_authorize_patch(self):
        """REGRESSION: a warning (e.g. cache miss rate) marked ACTIONABLE
        but without dependency evidence must NOT authorize the Jinja2
        patch. Only inbox_app_warning is ACTIONABLE; all others BENIGN."""
        def triage_fn(key):
            if key == "inbox_app_warning":
                return make_triage("ACTIONABLE", advisory_metadata={})(key)
            return make_triage("BENIGN", advisory_metadata={})(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(out["summary"]["remediation_eligible"], 0)

    def test_only_one_recovery_runs_uncertain_signals_remain_separate(self):
        """Even with multiple eligible signals, only one recovery starts;
        uncertain signals remain separate handoffs."""
        call_count = 0

        def triage_fn(key):
            # All 5 are ACTIONABLE, but only inbox_dep_scan has dependency
            # evidence in its log text. The others are investigate-only.
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
        # Only inbox_dep_scan has dependency evidence in its log text.
        self.assertEqual(out["summary"]["remediation_eligible"], 1)
        self.assertEqual(call_count, 1)
        # The other 4 are investigate-only (ACTIONABLE but no dep evidence).
        self.assertEqual(len(out["investigate_signals"]), 4)

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


class TestEvidenceReachesEngineer(unittest.TestCase):
    """The selected dependency evidence must reach Engineer via the
    workflow's sentry_fn — the workflow must not re-triage from scratch."""

    def test_selected_evidence_reaches_engineer(self):
        """REGRESSION: process_inbox must pass the selected signal's
        triage result into run_incident via sentry_fn, so the workflow
        does NOT re-triage from scratch."""
        sentry_fn_received = {}

        def triage_fn(key):
            m = {"CVE-2024-34064": {}} if key == "inbox_dep_scan" else {}
            return make_triage("ACTIONABLE", advisory_metadata=m,
                               reason=f"triage-result-for-{key}")(key)

        def tracking_workflow(**kw):
            sentry_fn_received.update(kw)
            return {"id": kw.get("incident_id"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=tracking_workflow,
                               fault_inject_first=False)
        self.assertTrue(out["summary"]["recovery_started"])
        # The workflow must have received a sentry_fn kwarg.
        self.assertIn("sentry_fn", sentry_fn_received)
        # Calling that sentry_fn must return the inbox's triage result,
        # not re-triage from scratch.
        bound_fn = sentry_fn_received["sentry_fn"]
        result = bound_fn("inbox_dep_scan")
        self.assertEqual(result["reason"], "triage-result-for-inbox_dep_scan")


class TestHandoffPreservation(unittest.TestCase):
    """Handoffs must preserve original reason, override, and null timing."""

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

    def test_inbox_results_record_has_dependency_evidence(self):
        def triage_fn(key):
            m = {"CVE": {}} if key == "inbox_dep_scan" else {}
            return make_triage("BENIGN", advisory_metadata=m)(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        dep = next(r for r in out["inbox_results"] if r["log_key"] == "inbox_dep_scan")
        self.assertTrue(dep["has_dependency_evidence"])
        err = next(r for r in out["inbox_results"] if r["log_key"] == "inbox_app_error")
        self.assertFalse(err["has_dependency_evidence"])


class TestClassificationMismatches(unittest.TestCase):
    """Classification mismatches between expected and actual are tracked."""

    def test_mismatch_recorded_when_model_disagrees_with_expected(self):
        def triage_fn(key):
            # Model says ACTIONABLE for the app error, but expected is BENIGN.
            return make_triage("ACTIONABLE", advisory_metadata={})(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        mismatches = out["classification_mismatches"]
        app_err = [m for m in mismatches if m["log_key"] == "inbox_app_error"]
        self.assertTrue(len(app_err) > 0,
                        f"expected mismatch for inbox_app_error, got {mismatches}")
        self.assertEqual(app_err[0]["expected"], "BENIGN")
        self.assertEqual(app_err[0]["actual"], "ACTIONABLE")


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
        text = json.dumps(out).lower()
        for word in ("attack occurred", "proves exploitability",
                     "confirmed exploit"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
