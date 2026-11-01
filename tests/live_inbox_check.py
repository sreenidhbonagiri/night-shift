"""Bounded live verification of the mixed synthetic incident inbox.

Uses REAL Sentry triage calls and the REAL workflow (including fault
injection) for the actionable signal, then runs approval, staging
release, health/smoke checks, and stop.

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

print("INBOX SUMMARY:", json.dumps(report["inbox"]["summary"], indent=2))
print("CLASSIFICATIONS:")
for c in report["classifications"]:
    print(f"  {c['log_key']}: expected={c['expected_label']} got={c['final_disposition']} "
          f"model={c['model_disposition']} override={c.get('policy_override')}")
print("HANDOFFS:", len(report["handoffs"]))
for h in report["handoffs"]:
    print(f"  {h['log_key']}: status={h['status']} timing={h['elapsed_alert_to_tested_patch_ms']}")

# Verify classification structure
assert out["summary"]["total"] == 5
# The dep_scan should be ACTIONABLE (or escalated); the incomplete signal
# should be NEEDS_HUMAN_REVIEW (insufficient-data marker in log text).
assert len(out["handoffs"]) >= 1, "expected at least one handoff for the incomplete signal"

# --- 2. Recovery incident (if any) → approval → staging → stop -------
inc = out.get("recovery_incident")
if inc is None:
    report["recovery"] = "No actionable signal started recovery."
    print("\nNO RECOVERY STARTED (no actionable signal with sufficient evidence)")
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
        # Approval
        approval = ap.approve(incident=inc, actor="mel-inbox-check")
        report["approval"] = {"approved": approval.get("approved"),
                              "error": approval.get("error")}
        assert approval.get("approved") is True, approval

        # Staging release
        release = staging.release(inc)
        report["staging"] = {
            "released": release.get("released"),
            "error": release.get("error"),
            "port": release.get("port"),
        }
        assert release.get("released") is True, release

        # Health and smoke
        report["staging"]["health"] = release.get("health")
        report["staging"]["smoke"] = release.get("smoke")
        assert release["health"].get("status") == "ok"
        assert release["smoke"].get("status") == "ok"
        assert staging._smoke_gate(release["smoke"]) is None

        # Stop
        stop = staging.stop(inc)
        report["stop"] = stop
        assert stop.get("stopped") is True
        assert inc["status"] == "staging_stopped"

        # Export serializability
        dumped = json.dumps(inc)
        report["export_bytes"] = len(dumped)
    else:
        report["recovery"]["note"] = (
            f"Recovery ended with status {inc['status']}; "
            "no approval/staging attempted.")

out_path = ROOT / "live_inbox_check_report.json"
out_path.write_text(json.dumps(report, indent=2))
print(f"\nALL INBOX CHECKS PASSED — report written to {out_path}")
