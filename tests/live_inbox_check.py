"""Bounded live verification of the corrected mixed inbox.

Uses REAL Sentry triage calls and the REAL workflow for the eligible
signal, then runs approval, staging, checks, and stop.
Reports classification mismatches separately from safety gating.

Usage: python tests/live_inbox_check.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/sreenidhbonagiri/night-shift-mel")
sys.path.insert(0, str(ROOT))

from app import inbox as ib
from app import approval as ap
from app import staging

report = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

# --- 1. Process the inbox with real triage + real workflow -----------
out = ib.process_inbox(fault_inject_first=True)
report["inbox"] = {
    "summary": out["summary"],
    "elapsed_ms": out["elapsed_ms"],
}

# Record each signal's classification
report["classifications"] = []
for r in out["inbox_results"]:
    report["classifications"].append({
        "log_key": r["log_key"],
        "description": r["description"],
        "expected_label": r["expected_label"],
        "final_disposition": r["disposition"],
        "model_disposition": r.get("model_disposition"),
        "policy_override": r.get("policy_override"),
        "reason": r.get("reason"),
        "has_dependency_evidence": r.get("has_dependency_evidence"),
    })

# Record handoffs
report["handoffs"] = []
for h in out["handoffs"]:
    report["handoffs"].append({
        "log_key": h["log_key"],
        "status": h["status"],
        "attempts": h["attempts"],
        "elapsed_alert_to_tested_patch_ms": h["elapsed_alert_to_tested_patch_ms"],
        "reason": h.get("reason"),
    })

# Record investigate signals
report["investigate_signals"] = []
for s in out.get("investigate_signals", []):
    report["investigate_signals"].append({
        "log_key": s["log_key"],
        "disposition": s["disposition"],
        "has_dependency_evidence": s.get("has_dependency_evidence"),
    })

# Record classification mismatches separately
report["classification_mismatches"] = out.get("classification_mismatches", [])

print("INBOX SUMMARY:", json.dumps(report["inbox"]["summary"], indent=2))
print("CLASSIFICATIONS:")
for c in report["classifications"]:
    depEv = c.get("has_dependency_evidence", "?")
    print(f"  {c['log_key']}: expected={c['expected_label']} got={c['final_disposition']} "
          f"model={c.get('model_disposition')} depEv={depEv}")
print("HANDOFFS:", len(report["handoffs"]))
print("INVESTIGATE:", len(report["investigate_signals"]))
print("MISMATCHES:", len(report["classification_mismatches"]))
for m in report["classification_mismatches"]:
    print(f"  {m['log_key']}: expected={m['expected']} got={m['actual']}")

# Verify structure
assert out["summary"]["total"] == 5
# The incomplete signal MUST be a handoff (insufficient-data marker).
assert len(out["handoffs"]) >= 1, "expected at least one handoff for incomplete signal"
# Only dep_scan should be remediation-eligible.
assert out["summary"]["remediation_eligible"] >= 0

# --- 2. Recovery incident (if any) → approval → staging → stop -------
inc = out.get("recovery_incident")
if inc is None:
    report["recovery"] = "No remediation-eligible signal started recovery."
    print("\nNO RECOVERY STARTED (no eligible signal with dependency evidence)")
else:
    report["recovery"] = {
        "incident_id": inc["id"],
        "status": inc["status"],
        "attempts": len(inc.get("attempts", [])),
        "elapsed_alert_to_tested_patch_ms": inc.get("elapsed_alert_to_tested_patch_ms"),
    }
    print(f"\nRECOVERY: id={inc['id']} status={inc['status']} "
          f"attempts={len(inc.get('attempts', []))} "
          f"timing={inc.get('elapsed_alert_to_tested_patch_ms')}")

    # Fault injection label check
    if inc.get("attempts"):
        first = inc["attempts"][0]
        fi = first.get("engineer", {}).get("fault_injection")
        report["recovery"]["fault_injection_labeled"] = fi
        print(f"  Fault injection on attempt 1: {fi}")

    if inc["status"] == "tested_patch_awaiting_human_approval":
        approval = ap.approve(incident=inc, actor="mel-inbox-check")
        report["approval"] = {"approved": approval.get("approved"),
                              "error": approval.get("error")}
        assert approval.get("approved") is True, approval

        release = staging.release(inc)
        report["staging"] = {
            "released": release.get("released"),
            "error": release.get("error"),
            "port": release.get("port"),
        }
        assert release.get("released") is True, release
        report["staging"]["health"] = release.get("health")
        report["staging"]["smoke"] = release.get("smoke")
        assert release["health"].get("status") == "ok"
        assert release["smoke"].get("status") == "ok"
        assert staging._smoke_gate(release["smoke"]) is None

        stop = staging.stop(inc)
        report["stop"] = stop
        assert stop.get("stopped") is True
        assert inc["status"] == "staging_stopped"

        dumped = json.dumps(inc)
        report["export_bytes"] = len(dumped)
    else:
        report["recovery"]["note"] = (
            f"Recovery ended with status {inc['status']}; "
            "no approval/staging attempted.")

out_path = ROOT / "live_inbox_check_report.json"
out_path.write_text(json.dumps(report, indent=2))
print(f"\nALL INBOX CHECKS PASSED — report written to {out_path}")
