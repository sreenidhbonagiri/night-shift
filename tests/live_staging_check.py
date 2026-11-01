"""Bounded live staging verification: ONE real recovery → approval → real
local staging → health/smoke → stop.

Real model (Ollama qwen3:4b), real disposable-venv installs, real staging
process on 127.0.0.1. Run manually:
    python tests/live_staging_check.py
Requires the Night Shift server on localhost:8000 and Ollama on 11434.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def get(path, timeout=300):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.load(r)


def post(path, timeout=300):
    req = urllib.request.Request(f"{BASE}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main():
    print("Waiting for server…")
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("[FAIL] Server not reachable at", BASE)
        return 1

    print("Starting live workflow (real model + disposable venv install)…")
    start = time.perf_counter()
    start_resp = post("/workflow")
    inc_id = start_resp["id"]
    print("Incident ID:", inc_id)
    print("Status: running (background)")

    # Poll for completion.
    for _ in range(120):
        st = get(f"/workflow/status/{inc_id}")
        if not st.get("running"):
            break
        time.sleep(2)
    else:
        print("[FAIL] Workflow did not complete within 240s.")
        return 1

    wall = time.perf_counter() - start
    incident = get(f"/incident/{inc_id}")
    print(f"Workflow wall time: {wall:.1f}s (state={incident['status']})")
    for a in incident.get("attempts", []):
        eng = a["engineer"]
        ver = a.get("verification") or {}
        qa = a.get("qa") or {}
        prop = eng.get("proposal") or {}
        print(f"  Attempt {a['attempt']}: "
              f"{prop.get('package','?')}"
              f"=={prop.get('version','?')} "
              f"rc={ver.get('pytest_returncode')} "
              f"failures={ver.get('junit',{}).get('failures')} "
              f"QA={qa.get('approval')}")
        if eng.get("investigation"):
            inv = eng["investigation"]
            print(f"    files_read: {inv.get('files_read')}")
            print(f"    declared: {inv.get('declared_version')} / {inv.get('fixture_declared_version')}")
            print(f"    grounding_problems: {inv.get('grounding_problems')}")
            print(f"    citation_problems: {inv.get('citation_problems')}")
    if incident["status"] != "tested_patch_awaiting_human_approval":
        print("[FAIL] Workflow did not produce an approvable patch.")
        return 1

    print("Approving…")
    approval = post(f"/incident/{inc_id}/approve")
    print("Approval:", approval.get("approved"), approval.get("error", ""))
    if not approval.get("approved"):
        print("[FAIL] Approval failed.")
        return 1

    print("Releasing approved artifact to REAL local staging…")
    release = post(f"/incident/{inc_id}/staging-release")
    print("Release:", release.get("released"),
          release.get("error", ""))
    if not release.get("released"):
        print("[FAIL] Staging release failed.")
        return 1
    port = release["port"]
    print(f"  version: {release['version']}")
    print(f"  port: {port}")
    print(f"  health: {json.dumps(release['health'])}")
    print(f"  smoke: {json.dumps(release['smoke'])}")
    if release["health"]["jinja2"] != release["version"]:
        print("[FAIL] Health jinja2 version mismatch.")
        return 1

    print("Status while running…")
    status = get(f"/incident/{inc_id}/staging-status")
    print(f"  {json.dumps(status)}")
    if not status["running"]:
        print("[FAIL] Staging should report running.")
        return 1

    print("Duplicate release prevention…")
    dup = post(f"/incident/{inc_id}/staging-release")
    print("  second release:", dup.get("released"), "-", dup.get("error", ""))
    if dup.get("released"):
        print("[FAIL] Duplicate staging was allowed.")
        return 1

    print("Direct endpoint checks from outside the app…")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        direct_health = json.load(r)
    print(f"  /health: {json.dumps(direct_health)}")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/smoke", timeout=10) as r:
        direct_smoke = json.load(r)
    print(f"  /smoke: {json.dumps(direct_smoke)}")
    if direct_health["status"] != "ok" or direct_smoke["status"] != "ok":
        print("[FAIL] Direct health/smoke check failed.")
        return 1

    print("Stopping staging…")
    stop = post(f"/incident/{inc_id}/staging-stop")
    print("Stop:", stop.get("stopped"), stop.get("error", ""))
    if not stop.get("stopped"):
        print("[FAIL] Stop failed.")
        return 1
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        print("[FAIL] Staging server still reachable after stop.")
        return 1
    except Exception:
        print("  port closed after stop: confirmed")

    print("Export contains staging events…")
    export = get(f"/export/{inc_id}")
    inc = export["incident"]
    actions = [e.get("action") for e in inc.get("events", [])
               if e.get("agent") == "staging"]
    print("  staging events:", actions)
    for expected in ("release_succeeded", "stop"):
        if expected not in actions:
            print(f"[FAIL] Export missing staging event {expected!r}.")
            return 1
    if "staging_release" not in inc:
        print("[FAIL] Export missing staging_release record.")
        return 1

    print()
    print("[LIVE STAGING PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
