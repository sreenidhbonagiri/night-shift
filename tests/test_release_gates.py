"""Regression tests for Fix 4: release and verification gates.

Each test reproduces an independently reproduced failure in the published
build BEFORE the fix:
  1. Approval records whose incident_id does not match the current
     incident were accepted at release time.
  2. Approval/release attempts with verification=None or qa=None were not
     explicitly/safely rejected at the gate boundary.
  3. The evidence evaluator trusted a junit dict whose aggregate counts
     were missing, non-integer, negative, or inconsistent with the
     testcase outcomes, and accepted unknown outcome values.
  4. The staging smoke gate accepted status "ok" without every expected
     check present and strictly True.
"""
import copy
import unittest

from app import approval as ap
from app import evidence as evidence_mod
from app.staging import _smoke_gate


def good_verification(**overrides):
    v = {
        "install_ok": True,
        "version_inspection_ok": True,
        "installed_version": "3.1.4",
        "proposed_version": "3.1.4",
        "pytest_returncode": 0,
        "junit": {
            "tests": {
                "test_render_greeting_escapes_html": "passed",
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            },
            "total": 4,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
        },
        "required_tests_ok": True,
        "success": True,
        "error": None,
        "artifact_sha256": "a" * 64,
        "artifact_dir": "/tmp/artifacts/x",
        "manifest": {"requirements-affected.txt": "b" * 64},
    }
    v.update(overrides)
    return v


def good_attempt():
    return {
        "attempt": 1,
        "engineer": {"proposal": {"package": "jinja2", "version": "3.1.4"},
                     "reason": "upgrade", "model": "mock", "elapsed_ms": 1.0,
                     "utc_timestamp": "t", "error": None},
        "verification": good_verification(),
        "qa": {"approval": True, "reason": "ok", "model": "mock",
               "elapsed_ms": 1.0, "utc_timestamp": "t", "error": None},
    }


def good_incident():
    return {
        "id": "inc-1",
        "status": "tested_patch_awaiting_human_approval",
        "events": [],
        "attempts": [good_attempt()],
        "verified_patch": {
            "package": "jinja2",
            "version": "3.1.4",
            "artifact_sha256": "a" * 64,
            "artifact_dir": "/tmp/artifacts/x",
            "manifest": {"requirements-affected.txt": "b" * 64},
        },
    }


class TestIncidentBindingGate(unittest.TestCase):
    def test_approval_with_mismatched_incident_id_rejected(self):
        """REGRESSION: an approval record bound to a different incident
        must not authorize release of this one."""
        inc = good_incident()
        # Fake a persisted human_approval from a DIFFERENT incident.
        inc["human_approval"] = {
            "approved": True, "actor": "human",
            "incident_id": "inc-OTHER",
            "patch": {"package": "jinja2", "version": "3.1.4"},
            "artifact_sha256": "a" * 64,
            "manifest_recheck": "passed",
            "utc_timestamp": "t",
        }
        out = ap.simulate_deploy(inc)
        self.assertFalse(out["simulated"])
        self.assertIn("incident", out["error"].lower())

    def test_simulate_deploy_rechecks_approval_incident_id(self):
        """Even a non-transferable approval must be verified as belonging
        to THIS incident before release."""
        inc = good_incident()
        inc["human_approval"] = {
            "approved": True, "actor": "human",
            "incident_id": "inc-OTHER",
            "patch": {"package": "jinja2", "version": "3.1.4"},
            "artifact_sha256": "a" * 64,
            "manifest_recheck": "passed",
            "utc_timestamp": "t",
        }
        out = ap.simulate_deploy(inc)
        self.assertFalse(out["simulated"])
        self.assertIn("inc-OTHER", out["error"])
        self.assertNotEqual(inc.get("status"), "simulated_deployed")


class TestNoneEvidenceRejection(unittest.TestCase):
    def test_approve_rejects_verification_none(self):
        """REGRESSION: an attempt with verification=None must be rejected
        safely, without exception."""
        inc = good_incident()
        inc["attempts"][0]["verification"] = None
        out = ap.approve(incident=inc)
        self.assertFalse(out["approved"])

    def test_approve_rejects_qa_none(self):
        inc = good_incident()
        inc["attempts"][0]["qa"] = None
        out = ap.approve(incident=inc)
        self.assertFalse(out["approved"])

    def test_simulate_deploy_rejects_verification_none(self):
        """A prior approval must not survive a verified patch whose
        verification is None."""
        inc = good_incident()
        inc["attempts"][0]["verification"] = None
        inc["human_approval"] = {
            "approved": True, "actor": "human", "incident_id": "inc-1",
            "patch": {"package": "jinja2", "version": "3.1.4"},
            "artifact_sha256": "a" * 64,
            "manifest_recheck": "passed", "utc_timestamp": "t",
        }
        out = ap.simulate_deploy(inc)
        self.assertFalse(out["simulated"])

    def test_simulate_deploy_rejects_qa_none(self):
        inc = good_incident()
        inc["attempts"][0]["qa"] = None
        inc["human_approval"] = {
            "approved": True, "actor": "human", "incident_id": "inc-1",
            "patch": {"package": "jinja2", "version": "3.1.4"},
            "artifact_sha256": "a" * 64,
            "manifest_recheck": "passed", "utc_timestamp": "t",
        }
        out = ap.simulate_deploy(inc)
        self.assertFalse(out["simulated"])


class TestStrictJUnitAggregates(unittest.TestCase):
    def _junit(self, **overrides):
        j = {
            "tests": {
                "test_render_greeting_escapes_html": "passed",
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            },
            "total": 4, "failures": 0, "errors": 0, "skipped": 0,
        }
        j.update(overrides)
        return j

    def _verification(self, junit):
        return good_verification(junit=junit)

    def test_missing_aggregates_fail(self):
        """REGRESSION: a junit dict without aggregate counts must fail
        evidence evaluation, not pass."""
        v = self._verification({
            "tests": {
                "test_render_greeting_escapes_html": "passed",
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            }})
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_non_integer_aggregate_fails(self):
        for bad in ("4", 4.0, True, [4]):
            v = self._verification(self._junit(total=bad))
            self.assertFalse(evidence_mod.evidence_ok(v), f"total={bad!r} passed")

    def test_negative_aggregate_fails(self):
        v = self._verification(self._junit(failures=-1))
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_inconsistent_total_fails(self):
        """total must equal the number of testcase outcomes."""
        v = self._verification(self._junit(total=5))
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_inconsistent_failure_count_fails(self):
        """failures must equal the number of failed outcomes."""
        v = self._verification(self._junit(
            tests={
                "test_render_greeting_escapes_html": "failed",
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            },
            failures=0))
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_unknown_outcome_rejected(self):
        """REGRESSION: an unknown outcome value (not passed/failed/error/
        skipped) must be rejected, not silently treated as passing."""
        v = self._verification(self._junit(
            tests={
                "test_render_greeting_escapes_html": "PASSED",  # unknown casing/value
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            }))
        self.assertFalse(evidence_mod.evidence_ok(v))

    def test_none_status_rejected(self):
        v = self._verification(self._junit(
            tests={
                "test_render_greeting_escapes_html": None,
                "test_xmlattr_rejects_space_in_key": "passed",
                "test_xmlattr_rejects_slash_in_key": "passed",
                "test_xmlattr_renders_normal_attributes": "passed",
            }))
        self.assertFalse(evidence_mod.evidence_ok(v))


class TestSmokeGate(unittest.TestCase):
    def _payload(self, **overrides):
        p = {
            "status": "ok",
            "checks": {
                "greeting_escapes_html": True,
                "xmlattr_rejects_space": True,
                "xmlattr_rejects_slash": True,
            },
        }
        p.update(overrides)
        return p

    def test_ok_with_all_checks_true_passes(self):
        self.assertIsNone(_smoke_gate(self._payload()))

    def test_missing_check_rejected(self):
        """REGRESSION: status "ok" with a missing check must be rejected."""
        p = self._payload()
        del p["checks"]["xmlattr_rejects_slash"]
        reason = _smoke_gate(p)
        self.assertIsNotNone(reason)
        self.assertIn("xmlattr_rejects_slash", reason)

    def test_false_check_rejected(self):
        p = self._payload(checks={
            "greeting_escapes_html": True,
            "xmlattr_rejects_space": False,
            "xmlattr_rejects_slash": True})
        reason = _smoke_gate(p)
        self.assertIsNotNone(reason)
        self.assertIn("xmlattr_rejects_space", reason)

    def test_truthy_non_bool_check_rejected(self):
        """A truthy non-bool value (e.g. "yes", 1) must not pass as True."""
        p = self._payload(checks={
            "greeting_escapes_html": "yes",
            "xmlattr_rejects_space": True,
            "xmlattr_rejects_slash": True})
        reason = _smoke_gate(p)
        self.assertIsNotNone(reason)
        self.assertIn("greeting_escapes_html", reason)

    def test_null_check_rejected(self):
        p = self._payload(checks={
            "greeting_escapes_html": None,
            "xmlattr_rejects_space": True,
            "xmlattr_rejects_slash": True})
        reason = _smoke_gate(p)
        self.assertIsNotNone(reason)

    def test_extra_unknown_check_does_not_block(self):
        p = self._payload()
        p["checks"]["extra_diagnostic"] = False
        # Unknown extras are informational; only expected checks gate.
        self.assertIsNone(_smoke_gate(p))

    def test_status_not_ok_rejected(self):
        p = self._payload(status="failed")
        self.assertIsNotNone(_smoke_gate(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
