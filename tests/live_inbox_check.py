"""Bounded live verification of the corrected mixed inbox.

Uses REAL Sentry triage calls and the REAL workflow for the eligible
signal, then runs approval, staging, checks, and stop.

Reports three dimensions SEPARATELY and honestly:
  1. Classification accuracy (model disposition vs expected).
  2. Routing correctness (policy route vs expected).
  3. Remediation success (recovery, approval, staging, checks, stop).

The check does NOT declare everything passed while classification
assertions failed: the final verdict reports each dimension's pass/fail
independently.

Usage: python tests/live_inbox_check.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import inbox as ib
from app import approval as ap
from app import staging

report = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

# --- 1. Process the inbox with real triage + real workflow -----------
out = ib.process_inbox(fault_inject_first=True)
s = out["summary"]
report["inbox"] = {"summary": s, "elapsed_ms": out["elapsed_ms"]}

report["classifications"] = []
for r in out["inbox_results"]:
    report["classifications"].append({
        "log_key": r["log_key"],
        "description": r["description"],
        "expected_disposition": r["expected_disposition"],
        "expected_route": r["expected_route"],
        "model_disposition": r["disposition"],
        "policy_route": r["policy_route"],
        "policy_override": r.get("policy_override"),
        "reason": r.get("reason"),
        "patch_eligibility": r.get("patch_eligibility"),
    })

report["handoffs"] = [
    {"log_key": h["log_key"], "status": h["status"], "attempts": h["attempts"],
     "elapsed_alert_to_tested_patch_ms": h["elapsed_alert_to_tested_patch_ms"],
     "reason": h.get("reason")}
    for h in out["handoffs"]]

print("INBOX SUMMARY:", json.dumps(s, indent=2))
print("CLASSIFICATIONS:")
for c in report["classifications"]:
    pe = c["patch_eligibility"] or {}
    print(f"  {c['log_key']}: model={c['model_disposition']} "
          f"(expected {c['expected_disposition']}) | "
          f"route={c['policy_route']} (expected {c['expected_route']}) | "
          f"eligible={pe.get('eligible')}")
print("HANDOFFS:", len(report["handoffs"]))
print("CLASSIFICATION MISMATCHES:", len(out["classification_mismatches"]))
for m in out["classification_mismatches"]:
    print(f"  {m['log_key']}: expected={m['expected']} got={m['actual']}")
print("ROUTING MISMATCHES:", len(out["routing_mismatches"]))
for m in out["routing_mismatches"]:
    print(f"  {m['log_key']}: expected={m['expected']} got={m['actual']}")

# --- Structural assertions (always required) --------------------------
assert s["total"] == 5
# The incomplete signal MUST route to human_review (insufficient-data
# marker in its log forces the policy override).
assert s["routes"]["human_review"] >= 1, "incomplete signal must be a handoff"
# Only patch-eligible signals may remediate; at most one workflow runs.
assert s["routes"]["remediate"] <= 1

# --- Dimension 1: classification accuracy ----------------------------
classification_pass = len(out["classification_mismatches"]) == 0
report["classification_accuracy"] = {
    "pass": classification_pass,
    "correct": s["total"] - len(out["classification_mismatches"]),
    "total": s["total"],
    "mismatches": out["classification_mismatches"],
}

# --- Dimension 2: routing correctness --------------------------------
routing_pass = len(out["routing_mismatches"]) == 0
report["routing_correctness"] = {
    "pass": routing_pass,
    "correct": s["total"] - len(out["routing_mismatches"]),
    "total": s["total"],
    "mismatches": out["routing_mismatches"],
}

# --- Dimension 3: remediation success --------------------------------
inc = out.get("recovery_incident")
remediation = {"started": inc is not None, "pass": False}
report["remediation"] = remediation
if inc is None:
    remediation["note"] = ("No patch-eligible signal; no recovery started "
                           "(this is correct if none were eligible).")
    remediation["pass"] = s["routes"]["remediate"] == 0
    print("\nNO RECOVERY STARTED (no patch-eligible signal)")
else:
    remediation["incident_id"] = inc["id"]
    remediation["status"] = inc["status"]
    remediation["attempts"] = len(inc.get("attempts", []))
    remediation["elapsed_alert_to_tested_patch_ms"] = inc.get(
        "elapsed_alert_to_tested_patch_ms")
    print(f"\nRECOVERY: id={inc['id']} status={inc['status']} "
          f"attempts={remediation['attempts']} "
          f"timing={remediation['elapsed_alert_to_tested_patch_ms']}")

    if inc.get("attempts"):
        fi = inc["attempts"][0].get("engineer", {}).get("fault_injection")
        remediation["fault_injection_labeled"] = fi
        print(f"  Fault injection on attempt 1: {fi}")

    if inc["status"] == "tested_patch_awaiting_human_approval":
        approval = ap.approve(incident=inc, actor="mel-inbox-check")
        remediation["approval"] = {"approved": approval.get("approved"),
                                   "error": approval.get("error")}
        assert approval.get("approved") is True, approval

        release = staging.release(inc)
        remediation["staging"] = {
            "released": release.get("released"),
            "error": release.get("error"),
            "port": release.get("port"),
        }
        assert release.get("released") is True, release
        remediation["staging"]["health"] = release.get("health")
        remediation["staging"]["smoke"] = release.get("smoke")
        assert release["health"].get("status") == "ok"
        assert release["smoke"].get("status") == "ok"
        assert staging._smoke_gate(release["smoke"]) is None

        stop = staging.stop(inc)
        remediation["stop"] = stop
        assert stop.get("stopped") is True
        assert inc["status"] == "staging_stopped"

        remediation["export_bytes"] = len(json.dumps(inc))
        remediation["pass"] = True
    else:
        remediation["note"] = (f"Recovery ended with status {inc['status']}; "
                               "no approval/staging attempted.")

# --- Final verdict: dimensions reported separately --------------------
print("\n=== DIMENSION VERDICTS ===")
print(f"Classification accuracy: {'PASS' if classification_pass else 'FAIL'} "
      f"({report['classification_accuracy']['correct']}/{report['classification_accuracy']['total']} correct)")
print(f"Routing correctness:     {'PASS' if routing_pass else 'FAIL'} "
      f"({report['routing_correctness']['correct']}/{report['routing_correctness']['total']} correct)")
print(f"Remediation success:     {'PASS' if remediation['pass'] else 'FAIL'}")

report["verdict"] = {
    "classification_accuracy": classification_pass,
    "routing_correctness": routing_pass,
    "remediation": remediation["pass"],
}
if not classification_pass:
    report["verdict"]["note"] = (
        "Classification accuracy FAILED: at least one model disposition "
        "differed from the expected disposition. This is reported as a "
        "failure regardless of routing or remediation outcomes.")

out_path = ROOT / "live_inbox_check_report.json"
out_path.write_text(json.dumps(report, indent=2))
print(f"\nReport written to {out_path}")
print("STRUCTURAL CHECKS PASSED (structure, gating safety, staging lifecycle).")
print("See dimension verdicts above for accuracy/routing/remediation results.")
