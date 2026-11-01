"""Bounded live verification of the five repaired failures (Fix batch).

Runs REAL operations, each bounded:
  1. One real recovery (fault-injection attempt + corrected patch) with
     polling that records intermediate events WHILE the workflow runs.
  2. Human approval -> staging release -> /health + /smoke -> stop ->
     export, asserting the honest stopped state.
  3. Real triage of the benign and ambiguous (uncertain) logs.

Usage: python tests/live_repair_check.py
"""
import json
import sys
import time
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import workflow as wf
from app import staging
from app import sentry_agent
from app import approval as ap

POLL_INTERVAL = 0.5
MAX_WAIT_S = 300.0


def main():
    report = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # --- 1. Real recovery with live-progress polling -------------------
    incident_id = str(uuid.uuid4())
    snapshots = []
    done = threading.Event()
    result = {}

    def progress_cb(events, attempts, status):
        snapshots.append({
            "t": round(time.monotonic(), 2),
            "status": status,
            "event_count": len(events),
            "attempt_count": len(attempts),
            "agents": sorted({e.get("agent") for e in events if e.get("agent")}),
        })

    def run():
        try:
            result["incident"] = wf.run_incident(
                log_key="actionable", fault_inject_first=True,
                incident_id=incident_id, progress_cb=progress_cb)
        except Exception as exc:  # pragma: no cover
            result["error"] = str(exc)
        finally:
            done.set()

    t0 = time.monotonic()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    saw_intermediate = False
    while not done.is_set() and time.monotonic() - t0 < MAX_WAIT_S:
        time.sleep(POLL_INTERVAL)
        # Poll the shared snapshots as a proxy for /workflow/status polling.
        if snapshots and snapshots[-1]["event_count"] >= 1 \
                and not done.is_set():
            saw_intermediate = True

    thread.join(timeout=10)
    if "error" in result:
        print("WORKFLOW ERROR:", result["error"])
        sys.exit(1)

    inc = result["incident"]
    report["recovery"] = {
        "incident_id": inc["id"],
        "status": inc["status"],
        "elapsed_alert_to_tested_patch_ms": inc.get("elapsed_alert_to_tested_patch_ms"),
        "attempts": len(inc.get("attempts", [])),
        "final_event_count": len(inc.get("events", [])),
        "progress_snapshots": snapshots,
        "saw_intermediate_events_while_running": saw_intermediate,
        "snapshot_count": len(snapshots),
    }
    print("RECOVERY:", json.dumps(report["recovery"], indent=2))
    assert inc["status"] == "tested_patch_awaiting_human_approval", inc["status"]
    assert inc.get("elapsed_alert_to_tested_patch_ms") is not None
    assert saw_intermediate, "no intermediate events observed while running"
    assert len(snapshots) >= 2, f"only {len(snapshots)} progress publications"

    # --- 2. Approval -> staging -> health/smoke -> stop -> export ------
    approval = ap.approve(incident=inc, actor="mel-live-check")
    report["approval"] = {"approved": approval.get("approved"),
                          "error": approval.get("error")}
    assert approval.get("approved") is True, approval

    release = staging.release(inc)
    report["staging"] = {
        "released": release.get("released"),
        "error": release.get("error"),
        "port": release.get("port"),
        "health": release.get("health"),
        "smoke": release.get("smoke"),
    }
    assert release.get("released") is True, release
    assert release["health"].get("status") == "ok"
    assert release["smoke"].get("status") == "ok"
    assert staging._smoke_gate(release["smoke"]) is None
    assert inc["status"] == "staging_released"

    stop = staging.stop(inc)
    report["stop"] = stop
    assert stop.get("stopped") is True, stop
    assert inc["status"] == "staging_stopped", inc["status"]

    # Export shape (incident JSON must be serializable and complete).
    exported = json.dumps(inc, indent=2)
    report["export_bytes"] = len(exported)
    assert inc["id"] in exported and "simulated_deployment" not in inc

    # --- 3. Real triage: benign and uncertain --------------------------
    benign = sentry_agent.triage("benign")
    ambiguous = sentry_agent.triage("ambiguous")
    report["triage_benign"] = {k: benign.get(k) for k in (
        "disposition", "model_disposition", "policy_override", "reason")}
    report["triage_ambiguous"] = {k: ambiguous.get(k) for k in (
        "disposition", "model_disposition", "policy_override", "reason")}
    print("TRIAGE BENIGN:", json.dumps(report["triage_benign"], indent=2))
    print("TRIAGE AMBIGUOUS:", json.dumps(report["triage_ambiguous"], indent=2))

    out_path = Path(__file__).resolve().parents[1] / "live_repair_check_report.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nALL LIVE CHECKS PASSED — report written to {out_path}")


if __name__ == "__main__":
    main()
