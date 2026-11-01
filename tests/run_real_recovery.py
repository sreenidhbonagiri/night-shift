"""Bounded real recovery: Git proposal → QA → approval → staging → stop.

Runs one end-to-end workflow against real Ollama, then exercises the full
approval/staging/stop/export chain including Git proposal binding.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.workflow import run_incident
from app.approval import approve
from app.staging import release as stage_release, stop as stage_stop
from app.main import _incidents, _store_lock

PRIVATE = Path(os.environ.get(
    "NS_PRIVATE_DIR",
    os.path.expanduser("~/night-shift-mel-private/evaluation")))


def main():
    print("=== Bounded real recovery (with Git) ===")
    t0 = time.monotonic()

    incident = run_incident(log_key="actionable", fault_inject_first=True)
    inc_id = incident["id"]
    status = incident["status"]

    # Store for API access
    with _store_lock:
        _incidents[inc_id] = incident

    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"Workflow: {status} ({elapsed_ms:.0f}ms)")
    print(f"Incident: {inc_id}")
    print(f"Attempts: {len(incident.get('attempts', []))}")

    if status != "tested_patch_awaiting_human_approval":
        print(f"ABORT: not in approval state: {status}")
        PRIVATE.mkdir(parents=True, exist_ok=True)
        (PRIVATE / "real-recovery-git-report.json").write_text(json.dumps({
            "status": "aborted", "incident": incident, "error": f"Not in approval state: {status}"
        }, indent=2))
        return

    # Show Git proposal
    gp = incident.get("git_proposal", {})
    if gp.get("verified"):
        print(f"Git branch: {gp.get('branch')}")
        print(f"Base commit: {gp.get('base_commit', '')[:12]}")
        print(f"Proposal commit: {gp.get('proposal_commit', '')[:12]}")
        print(f"Diff preview:\n{gp.get('diff', '')[:200]}")
    else:
        print(f"Git proposal error: {gp.get('error')}")

    # Human approval
    print("\n--- Approval ---")
    apr = approve(incident)
    print(f"Approved: {apr.get('approved')}")

    if not apr.get("approved"):
        print(f"Approval failed: {apr.get('error')}")
        return

    # Staging release
    print("\n--- Staging ---")
    rel = stage_release(incident)
    print(f"Released: {rel.get('released')}")
    if not rel.get("released"):
        print(f"Release error: {rel.get('error')}")
    else:
        health = rel.get("health", {})
        smoke = rel.get("smoke", {})
        print(f"Health: {health.get('status')}, Jinja2={health.get('jinja2')}")
        print(f"Smoke: {smoke.get('status')}, checks={list(smoke.get('checks', {}).keys())}")

        # Stop
        print("\n--- Stop ---")
        stp = stage_stop(incident)
        print(f"Stopped: {stp.get('stopped')}")

    # Export
    print("\n--- Export ---")
    from app.main import export_incident
    exp_resp = export_incident(inc_id)
    # export_incident now returns a JSONResponse; extract body.
    if hasattr(exp_resp, "body"):
        import json as _j
        body = exp_resp.body
        if isinstance(body, bytes):
            exp = _j.loads(body)
        else:
            exp = _j.loads(str(body))
    elif hasattr(exp_resp, "json") and callable(getattr(exp_resp, "json", None)):
        exp = exp_resp.json()
    else:
        exp = exp_resp
    has_git = bool(exp.get("incident", {}).get("git_proposal"))
    print(f"Export has git_proposal: {has_git}")
    print(f"Export has human_approval: {'human_approval' in exp.get('incident', {})}")
    print(f"Export has staging_release: {'staging_release' in exp.get('incident', {})}")

    # Save report
    PRIVATE.mkdir(parents=True, exist_ok=True)
    report = {
        "scenario": "real_recovery_with_git",
        "incident_id": inc_id,
        "workflow_status": status,
        "approval": apr.get("approved"),
        "release": rel.get("released"),
        "release_error": rel.get("error"),
        "stop": stp.get("stopped") if 'stp' in dir() else None,
        "git_proposal_present": "error" not in gp,
        "git_branch": gp.get("branch") if "error" not in gp else None,
        "git_proposal_commit": gp.get("proposal_commit", "")[:12] if "error" not in gp else None,
        "export_has_git": has_git,
        "elapsed_total_ms": round((time.monotonic() - t0) * 1000, 2),
    }
    (PRIVATE / "real-recovery-git-report.json").write_text(json.dumps(report, indent=2))
    print(f"\nReport saved to {PRIVATE / 'real-recovery-git-report.json'}")


if __name__ == "__main__":
    main()
