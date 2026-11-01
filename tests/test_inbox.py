"""Mock tests for the mixed synthetic incident inbox.

Tests cover classification policy, signal separation, remediation
gating, patch eligibility, and the Engineer evidence boundary — all
with injected triage/workflow/engineer functions, no model calls.
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


def noop_workflow(**kw):
    return {"id": kw.get("incident_id", "x"), "status": "blocked",
            "events": [], "attempts": [],
            "elapsed_alert_to_tested_patch_ms": None}


class TestEvaluationDimensions(unittest.TestCase):
    """Model disposition and policy route are separate enums."""

    def test_inbox_batch_has_five_signals(self):
        self.assertEqual(len(ib.INBOX_BATCH), 5)

    def test_expected_metadata_stored_not_in_fixture_text(self):
        """Expected dispositions and routes are evaluation metadata in
        INBOX_BATCH, never in the log text sent to the model."""
        for item in ib.INBOX_BATCH:
            log = sentry_agent.SYNTHETIC_FIXTURES[item["key"]]
            self.assertNotIn(item["expected_disposition"], log)
            self.assertNotIn(item["expected_route"], log)

    def test_inbox_app_error_expected_disposition_is_benign(self):
        """The harmless application-error fixture's expected model
        disposition must be BENIGN, never broadened."""
        item = next(i for i in ib.INBOX_BATCH if i["key"] == "inbox_app_error")
        self.assertEqual(item["expected_disposition"], "BENIGN")
        self.assertEqual(item["expected_route"], "dismiss")

    def test_inbox_app_warning_expected_route_is_investigate(self):
        """The application warning's expected ROUTE is investigate — the
        route enum, not compared against model dispositions."""
        item = next(i for i in ib.INBOX_BATCH if i["key"] == "inbox_app_warning")
        self.assertEqual(item["expected_route"], "investigate")

    def test_dimensions_reported_separately(self):
        out = ib.process_inbox(triage_fn=make_triage("BENIGN"),
                               workflow_fn=noop_workflow,
                               fault_inject_first=False)
        s = out["summary"]
        self.assertIn("dispositions", s)
        self.assertIn("routes", s)
        self.assertEqual(s["dispositions"]["BENIGN"], 5)
        self.assertEqual(s["routes"]["dismiss"], 5)


class TestPatchEligibility(unittest.TestCase):
    """Patch eligibility requires package + package-associated version
    within the advisory's affected range + matching trusted advisory."""

    def test_dep_scan_fixture_is_eligible(self):
        elig = ib._patch_eligibility(
            sentry_agent.SYNTHETIC_FIXTURES["inbox_dep_scan"])
        self.assertTrue(elig["eligible"], elig)
        self.assertEqual(elig["package"], "jinja2")
        self.assertEqual(elig["version"], "3.1.2")
        self.assertEqual(elig["advisory"], "cve-2024-34064")
        self.assertIsNone(elig["rejection_reason"])

    def test_generic_error_not_eligible(self):
        elig = ib._patch_eligibility(
            sentry_agent.SYNTHETIC_FIXTURES["inbox_app_error"])
        self.assertFalse(elig["eligible"])
        self.assertIn("supported package", elig["missing"])

    def test_warning_not_eligible(self):
        elig = ib._patch_eligibility(
            sentry_agent.SYNTHETIC_FIXTURES["inbox_app_warning"])
        self.assertFalse(elig["eligible"])

    def test_generic_marker_alone_not_eligible(self):
        """'affected by' or 'dependency scanner' alone cannot authorize
        Jinja2 remediation."""
        elig = ib._patch_eligibility(
            "msg='dependency scanner says something is affected by an issue'")
        self.assertFalse(elig["eligible"])

    def test_package_without_version_not_eligible(self):
        elig = ib._patch_eligibility("jinja2 is mentioned with cve-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertIn("package-associated version", elig["missing"])

    def test_unsupported_package_not_eligible(self):
        elig = ib._patch_eligibility("urllib3 1.26 affected by cve-2021-33503")
        self.assertFalse(elig["eligible"])
        self.assertIn("matching trusted advisory", elig["missing"])

    # --- Reproduced cases (the two bugs this fix addresses) ---

    def test_patched_jinja2_version_not_eligible(self):
        """REGRESSION: 'Jinja2 3.1.6 already patched for CVE-2024-34064'
        must NOT be eligible — 3.1.6 is NOT in the affected range
        (< 3.1.4)."""
        elig = ib._patch_eligibility(
            "Jinja2 3.1.6 already patched for CVE-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertEqual(elig["package"], "jinja2")
        self.assertEqual(elig["version"], "3.1.6")
        self.assertEqual(elig["advisory"], "cve-2024-34064")
        self.assertIn("version in affected range", elig["missing"])
        self.assertIn("not in advisory", elig["rejection_reason"])

    def test_unrelated_python_version_not_assigned_to_jinja2(self):
        """REGRESSION: 'Python 3.9.6 reports Jinja2 version unknown,
        CVE-2024-34064' must NOT assign 3.9.6 to Jinja2 — the version
        is not explicitly associated with the package."""
        elig = ib._patch_eligibility(
            "Python 3.9.6 reports Jinja2 version unknown, CVE-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertEqual(elig["package"], "jinja2")
        self.assertIsNone(elig["version"])
        self.assertIn("package-associated version", elig["missing"])
        self.assertIn("ambiguous", elig["rejection_reason"])

    # --- Affected-range coverage for each supported advisory ---

    def test_cve_2024_22195_affected_version_eligible(self):
        """CVE-2024-22195 affects < 3.1.3; jinja2 3.1.2 is eligible."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.2 affected by cve-2024-22195")
        self.assertTrue(elig["eligible"], elig)
        self.assertEqual(elig["advisory"], "cve-2024-22195")

    def test_cve_2024_22195_patched_version_not_eligible(self):
        """CVE-2024-22195 patched at 3.1.3; jinja2 3.1.3 is NOT
        eligible (already patched)."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.3 fixed for cve-2024-22195")
        self.assertFalse(elig["eligible"])
        self.assertIn("version in affected range", elig["missing"])

    def test_cve_2024_22195_later_version_not_eligible(self):
        """jinja2 3.1.5 is NOT in CVE-2024-22195's affected range
        (< 3.1.3)."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.5 with cve-2024-22195")
        self.assertFalse(elig["eligible"])
        self.assertIn("version in affected range", elig["missing"])

    def test_cve_2024_34064_affected_version_eligible(self):
        """CVE-2024-34064 affects < 3.1.4; jinja2 3.1.2 is eligible."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.2 affected by cve-2024-34064")
        self.assertTrue(elig["eligible"], elig)
        self.assertEqual(elig["advisory"], "cve-2024-34064")

    def test_cve_2024_34064_patched_version_not_eligible(self):
        """CVE-2024-34064 patched at 3.1.4; jinja2 3.1.4 is NOT
        eligible (already patched)."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.4 fixed for cve-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertIn("version in affected range", elig["missing"])

    def test_cve_2024_34064_later_version_not_eligible(self):
        """jinja2 3.1.6 is NOT in CVE-2024-34064's affected range
        (< 3.1.4)."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.6 with cve-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertIn("version in affected range", elig["missing"])

    # --- Missing / ambiguous package-version evidence ---

    def test_version_with_no_package_not_associated(self):
        """A bare version '3.1.2' without 'jinja2' nearby is ambiguous
        and does not satisfy the package-associated version check."""
        elig = ib._patch_eligibility(
            "scanner reports version 3.1.2 affected by cve-2024-34064")
        self.assertFalse(elig["eligible"])
        # No jinja2 in text → no package at all.
        self.assertIn("supported package", elig["missing"])

    def test_jinja2_mentioned_without_associated_version(self):
        """'jinja2 is vulnerable, CVE-2024-34064' has no version
        explicitly associated with jinja2 — not eligible."""
        elig = ib._patch_eligibility(
            "jinja2 is vulnerable, CVE-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertIn("package-associated version", elig["missing"])

    def test_contradictory_versions_not_eligible(self):
        """If the log says 'jinja2 3.1.4' (patched) but also mentions
        '3.1.2' elsewhere, only the package-associated version (3.1.4)
        is used — and it's not in the affected range."""
        elig = ib._patch_eligibility(
            "jinja2 3.1.4 but scanner also saw 3.1.2 cve-2024-34064")
        self.assertFalse(elig["eligible"])
        self.assertEqual(elig["version"], "3.1.4")
        self.assertIn("version in affected range", elig["missing"])


class TestRouting(unittest.TestCase):
    """Dispositions map to routes correctly."""

    def test_actionable_with_dep_evidence_routes_to_remediate(self):
        def triage_fn(key):
            if key == "inbox_dep_scan":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=noop_workflow,
                               fault_inject_first=False)
        self.assertEqual(out["summary"]["routes"]["remediate"], 1)
        self.assertIsNotNone(out["recovery_incident"])

    def test_generic_error_actionable_cannot_trigger_jinja2_remediation(self):
        """REGRESSION: an ACTIONABLE application error WITHOUT patch
        eligibility must NOT start the Jinja2 remediation workflow."""
        def triage_fn(key):
            if key == "inbox_app_error":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertEqual(out["summary"]["routes"]["remediate"], 0)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(len(out["investigate_signals"]), 1)
        # The misclassification is preserved as a failure. (The other
        # three signals' expected dispositions also differ from the
        # injected BENIGN, so 4 mismatches total is the honest count.)
        self.assertEqual(out["summary"]["classification_mismatches"], 4)
        app_err = [m for m in out["classification_mismatches"]
                   if m["log_key"] == "inbox_app_error"]
        self.assertEqual(app_err[0]["expected"], "BENIGN")
        self.assertEqual(app_err[0]["actual"], "ACTIONABLE")

    def test_unrelated_warning_cannot_authorize_patch(self):
        """REGRESSION: a warning (e.g. cache miss rate) marked ACTIONABLE
        but without patch eligibility must NOT authorize the Jinja2
        patch. It routes to investigate."""
        def triage_fn(key):
            if key == "inbox_app_warning":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertEqual(out["summary"]["routes"]["remediate"], 0)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(len(out["investigate_signals"]), 1)

    def test_uncertain_signal_routes_to_human_review(self):
        """A NEEDS_HUMAN_REVIEW signal must remain a separate handoff
        and never start the recovery workflow."""
        def triage_fn(key):
            return make_triage("NEEDS_HUMAN_REVIEW")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        self.assertEqual(out["summary"]["routes"]["human_review"], 5)
        self.assertFalse(out["summary"]["recovery_started"])
        self.assertEqual(len(out["handoffs"]), 5)
        for h in out["handoffs"]:
            self.assertEqual(h["attempts"], [])
            self.assertIsNone(h["elapsed_alert_to_tested_patch_ms"])

    def test_only_one_recovery_runs(self):
        """Even with all signals ACTIONABLE, only the patch-eligible one
        starts a workflow — and only one."""
        call_count = 0

        def counting_workflow(**kw):
            nonlocal call_count
            call_count += 1
            return {"id": kw.get("incident_id"), "status": "blocked",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=make_triage("ACTIONABLE"),
                               workflow_fn=counting_workflow,
                               fault_inject_first=False)
        self.assertEqual(call_count, 1)
        self.assertEqual(out["summary"]["routes"]["remediate"], 1)
        self.assertEqual(out["summary"]["routes"]["investigate"], 4)


class TestEngineerBoundary(unittest.TestCase):
    """The selected signal's evidence must reach Engineer's actual input
    — testing only the bound sentry_fn is insufficient."""

    def test_selected_evidence_reaches_engineer(self):
        """REGRESSION: the workflow calls engineer_fn(advisory_context,
        previous_rejection). The inbox must bind an engineer_fn whose
        context contains the selected signal's ID, original log, triage
        rationale, and advisory context — verified by capturing the
        actual engineer_fn arguments."""
        engineer_calls = []

        def capturing_engineer(advisory_context, previous_rejection=None):
            engineer_calls.append({
                "advisory_context": advisory_context,
                "previous_rejection": previous_rejection,
            })
            return {
                "proposal": {"package": "jinja2", "version": "3.1.4",
                             "reason": "mock"},
                "reason": "mock",
                "model": "mock",
                "elapsed_ms": 1.0,
                "utc_timestamp": "t",
                "error": None,
                "investigation": {
                    "needs_human_review": False,
                    "evidence": [],
                    "citation_problems": [],
                    "grounding_problems": [],
                },
            }

        def triage_fn(key):
            if key == "inbox_dep_scan":
                return make_triage(
                    "ACTIONABLE",
                    reason="scanner reports jinja2 3.1.2 CVE-2024-34064")(key)
            return make_triage("BENIGN")(key)

        # Minimal workflow shim that mirrors run_incident's engineer call
        # pattern (non-fault-injected path): engineer_fn(context, prev).
        def shim_workflow(**kw):
            engineer_fn = kw["engineer_fn"]
            sentry_fn = kw["sentry_fn"]
            sentry_result = sentry_fn(kw.get("log_key"))
            if sentry_result.get("disposition") != "ACTIONABLE":
                return {"id": kw.get("incident_id"), "status": "blocked",
                        "events": [], "attempts": [],
                        "elapsed_alert_to_tested_patch_ms": None}
            eng = engineer_fn("ADVISORY-CONTEXT-PLACEHOLDER", None)
            return {"id": kw.get("incident_id"), "status": "blocked",
                    "events": [{"agent": "engineer", **eng}],
                    "attempts": [{"attempt": 1, "engineer": eng}],
                    "elapsed_alert_to_tested_patch_ms": None}

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=shim_workflow,
                               engineer_fn=capturing_engineer,
                               fault_inject_first=False)
        self.assertTrue(out["summary"]["recovery_started"])
        self.assertGreaterEqual(len(engineer_calls), 1,
                                "engineer_fn was never called")

        ctx = engineer_calls[0]["advisory_context"]
        # Signal ID, original log, triage rationale, advisory context.
        self.assertIn("inbox_dep_scan", ctx)
        self.assertIn("Dependency scanner detected Jinja2 3.1.2", ctx)
        self.assertIn("scanner reports jinja2 3.1.2 CVE-2024-34064", ctx)
        self.assertIn("Sentry disposition: ACTIONABLE", ctx)
        self.assertIn("ADVISORY-CONTEXT-PLACEHOLDER", ctx)

    def test_bound_engineer_fn_passes_through_to_real_engineer(self):
        """The bound engineer_fn must delegate to the injected engineer
        with the enriched context, preserving the previous_rejection
        argument on retries."""
        captured = []

        def fake_engineer(context, previous_rejection=None):
            captured.append((context, previous_rejection))
            return {"proposal": None, "reason": "handoff",
                    "investigation": {"needs_human_review": True}}

        def triage_fn(key):
            if key == "inbox_dep_scan":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        rejection = {"reason": "3.1.2 rejected"}

        def shim_workflow(**kw):
            # Simulate a retry with a previous rejection.
            eng = kw["engineer_fn"]("ADVCTX", rejection)
            return {"id": kw.get("incident_id"), "status": "needs_human_review",
                    "events": [], "attempts": [],
                    "elapsed_alert_to_tested_patch_ms": None}

        ib.process_inbox(triage_fn=triage_fn,
                         workflow_fn=shim_workflow,
                         engineer_fn=fake_engineer,
                         fault_inject_first=False)
        self.assertEqual(len(captured), 1)
        ctx, prev = captured[0]
        self.assertIn("inbox_dep_scan", ctx)
        self.assertIn("ADVCTX", ctx)
        self.assertEqual(prev, rejection)


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
        def triage_fn(key):
            return make_triage("BENIGN",
                               advisory_metadata={"CVE-2024-34064": {"source": "ghsa"}})(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        for r in out["inbox_results"]:
            self.assertIn("advisory_metadata", r)
            self.assertIn("advisory_severity", r)


class TestMismatchTracking(unittest.TestCase):
    """Classification and routing mismatches are tracked separately."""

    def test_classification_mismatch_recorded_when_model_disagrees(self):
        def triage_fn(key):
            # Model says ACTIONABLE for the app error; expected BENIGN.
            if key == "inbox_app_error":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        mismatches = out["classification_mismatches"]
        app_err = [m for m in mismatches if m["log_key"] == "inbox_app_error"]
        self.assertTrue(app_err, f"expected mismatch for inbox_app_error, got {mismatches}")
        self.assertEqual(app_err[0]["expected"], "BENIGN")
        self.assertEqual(app_err[0]["actual"], "ACTIONABLE")

    def test_investigate_signal_routes_correctly_despite_mismatch(self):
        """A misclassified signal can still route correctly — the two
        dimensions are evaluated independently."""
        def triage_fn(key):
            if key == "inbox_app_error":
                return make_triage("ACTIONABLE")(key)
            return make_triage("BENIGN")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=MagicMock(),
                               fault_inject_first=False)
        # inbox_app_error: classification mismatch (expected BENIGN, got
        # ACTIONABLE) but routing is investigate (not remediate) — the
        # routing verdict for that signal is about safety, not accuracy.
        err = next(r for r in out["inbox_results"]
                   if r["log_key"] == "inbox_app_error")
        self.assertEqual(err["policy_route"], "investigate")
        self.assertEqual(err["expected_route"], "dismiss")
        # Routing mismatch is also honestly recorded.
        rm = [m for m in out["routing_mismatches"]
              if m["log_key"] == "inbox_app_error"]
        self.assertTrue(rm)


class TestNoAttackOrExploitabilityClaims(unittest.TestCase):
    """Inbox output must not claim an attack occurred or that logs prove
    exploitability."""

    def test_no_attack_claims_in_results(self):
        def triage_fn(key):
            return make_triage("ACTIONABLE")(key)

        out = ib.process_inbox(triage_fn=triage_fn,
                               workflow_fn=noop_workflow,
                               fault_inject_first=False)
        text = json.dumps(out).lower()
        for word in ("attack occurred", "proves exploitability",
                     "confirmed exploit"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
