"""Bounded live check: the ambiguous (insufficient-data) fixture must
escalate to NEEDS_HUMAN_REVIEW with the real model, and the full
downstream chain must hold: zero patch attempts, null alert-to-tested-
patch timing, approval rejected, release rejected.

Usage: python tests/live_ambiguous_check.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import sentry_agent, workflow as wf, approval as ap

report = {}

# --- Real triage on the ambiguous fixture ---
triage_out = sentry_agent.triage("ambiguous")
report["triage"] = {
    "synthetic_input": triage_out.get("log"),
    "final_disposition": triage_out.get("disposition"),
    "model_disposition": triage_out.get("model_disposition"),
    "model_assessed_severity": triage_out.get("model_assessed_severity"),
    "policy_override": triage_out.get("policy_override"),
    "reason": triage_out.get("reason"),
    "cited_evidence": json.loads(triage_out.get("raw", "{}")).get("cited_evidence"),
    "confidence": json.loads(triage_out.get("raw", "{}")).get("confidence"),
}
print("TRIAGE:", json.dumps(report["triage"], indent=2))

assert triage_out["disposition"] == "NEEDS_HUMAN_REVIEW", \
    f"expected NEEDS_HUMAN_REVIEW, got {triage_out['disposition']}"
assert triage_out["model_disposition"] is not None, "model_disposition not preserved"
assert triage_out.get("policy_override") is not None, "no policy_override recorded"

# --- Real workflow with the real triage (fault_inject_first=False so no
#     fault injection; the non-ACTIONABLE disposition blocks immediately) ---
inc = wf.run_incident(log_key="ambiguous", fault_inject_first=False,
                      sentry_fn=lambda key: triage_out)
report["workflow"] = {
    "status": inc["status"],
    "attempts": len(inc["attempts"]),
    "elapsed_alert_to_tested_patch_ms": inc.get("elapsed_alert_to_tested_patch_ms"),
    "events": [e.get("agent") for e in inc.get("events", [])],
}
print("WORKFLOW:", json.dumps(report["workflow"], indent=2))

assert inc["status"] == "blocked", f"expected blocked, got {inc['status']}"
assert len(inc["attempts"]) == 0, f"expected 0 attempts, got {len(inc['attempts'])}"
assert inc.get("elapsed_alert_to_tested_patch_ms") is None

# --- Approval rejected ---
ap_res = ap.approve(incident=inc)
report["approval"] = {"approved": ap_res.get("approved"), "error": ap_res.get("error")}
print("APPROVAL:", json.dumps(report["approval"], indent=2))
assert ap_res["approved"] is False
assert "human_approval" not in inc

# --- Release rejected ---
deploy_res = ap.simulate_deploy(inc)
report["release"] = {"simulated": deploy_res.get("simulated"), "error": deploy_res.get("error")}
print("RELEASE:", json.dumps(report["release"], indent=2))
assert deploy_res["simulated"] is False
assert "simulated_deployment" not in inc

out = ROOT / "live_ambiguous_check_report.json"
out.write_text(json.dumps(report, indent=2))
print(f"\nALL AMBIGUOUS CHECKS PASSED — report written to {out}")
