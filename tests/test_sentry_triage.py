"""Regression tests for Fix 3: Sentry triage honesty and robustness.

Each test reproduces an independently reproduced failure in the published
build BEFORE the fix:
  1. The synthetic log text fed to the model contained the expected-answer
     label (e.g. "[SYNTHETIC — ACTIONABLE]"), leaking the answer.
  2. A BENIGN disposition with weak evidence stopped remediation without
     human escalation; BENIGN substituted for uncertainty.
  3. Malformed model output (JSON arrays, nulls, missing fields) raised
     exceptions or misclassified instead of escalating safely.
"""
import json
import unittest
from unittest.mock import patch

from app import sentry_agent


def model_result(response, raw=None):
    return {"success": True, "response": response,
            "raw": raw or str(response), "elapsed_ms": 1.0, "error": None}


class TestNoExpectedAnswerLeak(unittest.TestCase):
    def test_model_input_contains_no_scenario_label(self):
        """REGRESSION: the log line sent to the model must not contain the
        ACTIONABLE/BENIGN/AMBIGUOUS expected-answer label."""
        captured = {}

        def fake_call_model(system_prompt, user_prompt, **kw):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return model_result({"disposition": "NEEDS_HUMAN_REVIEW",
                                 "severity": "INFO",
                                 "operational_priority": "NONE",
                                 "reason": "mock"})

        with patch.object(sentry_agent, "call_model", side_effect=fake_call_model):
            sentry_agent.triage("actionable")

        user_prompt = captured["user"]
        for label in ("ACTIONABLE", "BENIGN", "AMBIGUOUS"):
            self.assertNotIn(label, user_prompt,
                             f"expected-answer label '{label}' leaked into model input")
        # The log content itself must still be present.
        self.assertIn("payment-gateway", user_prompt)
        self.assertIn("CVE-2024-22195", user_prompt)

    def test_synthetic_provenance_retained_separately(self):
        """The output must still record synthetic provenance and the
        scenario key — just not in the model input."""
        with patch.object(sentry_agent, "call_model",
                          return_value=model_result(
                              {"disposition": "BENIGN", "severity": "INFO",
                               "operational_priority": "NONE",
                               "reason": "clear false positive"})):
            out = sentry_agent.triage("benign")
        self.assertEqual(out["log_key"], "benign")
        self.assertTrue(out.get("synthetic"))
        self.assertIn("SYNTHETIC", out.get("log", ""))


class TestEvidenceMinimums(unittest.TestCase):
    def _triage_with(self, response):
        with patch.object(sentry_agent, "call_model",
                          return_value=model_result(response)):
            return sentry_agent.triage("actionable")

    def _triage_raw(self, log_key, response, raw=None):
        with patch.object(sentry_agent, "call_model",
                          return_value=model_result(response, raw)):
            return sentry_agent.triage(log_key)

    def test_benign_with_insufficient_evidence_escalates(self):
        """REGRESSION: BENIGN must not substitute for uncertainty — with
        no supporting evidence fields, escalate to NEEDS_HUMAN_REVIEW."""
        out = self._triage_with({"disposition": "BENIGN", "severity": "NONE",
                                 "operational_priority": "NONE",
                                 "reason": "probably fine"})
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")
        # Original model assessment preserved.
        self.assertEqual(out["model_disposition"], "BENIGN")
        # Policy override reason recorded.
        self.assertIn("evidence", out["policy_override"].lower())
        self.assertIn("insufficient", out["policy_override"].lower())

    def test_actionable_with_insufficient_evidence_escalates(self):
        """Minimum evidence applies to automatic remediation in both
        directions: ACTIONABLE without evidence also escalates."""
        out = self._triage_with({"disposition": "ACTIONABLE", "severity": "NONE",
                                 "operational_priority": "NONE",
                                 "reason": "hunch"})
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")
        self.assertEqual(out["model_disposition"], "ACTIONABLE")
        self.assertIn("insufficient", out["policy_override"].lower())

    def test_actionable_with_sufficient_evidence_passes(self):
        out = self._triage_with(
            {"disposition": "ACTIONABLE", "severity": "WARNING",
             "operational_priority": "HIGH",
             "reason": "Scanner reports affected Jinja2 with CVE id.",
             "cited_evidence": ["CVE-2024-22195"],
             "confidence": 0.9})
        self.assertEqual(out["disposition"], "ACTIONABLE")
        self.assertIsNone(out.get("policy_override"))

    def test_benign_with_strong_evidence_passes(self):
        out = self._triage_with(
            {"disposition": "BENIGN", "severity": "NONE",
             "operational_priority": "NONE",
             "reason": "Scheduled health check completed successfully.",
             "cited_evidence": ["level=INFO", "scheduled"],
             "confidence": 0.9})
        self.assertEqual(out["disposition"], "BENIGN")
        self.assertIsNone(out.get("policy_override"))

    def test_benign_on_insufficient_data_log_escalates(self):
        """REGRESSION (independently reproduced live failure): the
        ambiguous fixture's log text itself states 'insufficient data to
        classify'. The model returned BENIGN, cited that text as evidence,
        and self-reported confidence 0.95 — clearing the evidence gate and
        yielding final BENIGN. A disposition of certainty (BENIGN or
        ACTIONABLE) on a log that admits insufficient data must escalate:
        citing the log's own 'insufficient data' text is not confirmatory
        evidence, it is the signal of uncertainty."""
        # Exact response from the live model run on the ambiguous fixture.
        raw = json.dumps({
            "disposition": "BENIGN", "severity": "INFO",
            "operational_priority": "LOW",
            "reason": ("The log indicates a possible dependency issue but "
                       "lacks specific details for classification, which "
                       "is a common false positive in monitoring systems."),
            "cited_evidence": ["Possible dependency issue; insufficient data to classify"],
            "confidence": 0.95,
        })
        out = self._triage_raw("ambiguous", json.loads(raw), raw)
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")
        self.assertEqual(out["model_disposition"], "BENIGN")
        self.assertIsNotNone(out.get("policy_override"))
        self.assertIn("insufficient", out["policy_override"].lower())

    def test_actionable_on_insufficient_data_log_escalates(self):
        """Same uncertainty rule applies in the ACTIONABLE direction."""
        out = self._triage_raw("ambiguous", {
            "disposition": "ACTIONABLE", "severity": "WARNING",
            "operational_priority": "HIGH",
            "reason": "Possible dependency issue; insufficient data to classify.",
            "cited_evidence": ["Possible dependency issue; insufficient data to classify"],
            "confidence": 0.95,
        })
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")
        self.assertEqual(out["model_disposition"], "ACTIONABLE")
        self.assertIsNotNone(out.get("policy_override"))


class TestRobustModelOutput(unittest.TestCase):
    def _triage_with(self, response, raw=None):
        with patch.object(sentry_agent, "call_model",
                          return_value=model_result(response, raw)):
            return sentry_agent.triage("actionable")

    def test_json_array_escalates_without_exception(self):
        """REGRESSION: a JSON array response must not raise; it must
        escalate to NEEDS_HUMAN_REVIEW."""
        out = self._triage_with(["disposition", "BENIGN"])
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")
        self.assertIn("Malformed model output", out["reason"])

    def test_null_values_escalate_without_exception(self):
        out = self._triage_with({"disposition": None, "severity": None,
                                 "operational_priority": None, "reason": None})
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")

    def test_missing_fields_escalate_without_exception(self):
        out = self._triage_with({})
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")

    def test_null_nested_reason_handled(self):
        out = self._triage_with({"disposition": "ACTIONABLE",
                                 "reason": None})
        self.assertEqual(out["disposition"], "NEEDS_HUMAN_REVIEW")

    def test_original_assessment_preserved_on_malformed(self):
        raw = '["disposition", "BENIGN"]'
        out = self._triage_with(["disposition", "BENIGN"], raw=raw)
        self.assertEqual(out["error"], "Malformed model output")
        self.assertEqual(out["raw"], raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
