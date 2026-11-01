"""Bounded live verification: ONE real recovery workflow run.

Uses the real Ollama model (qwen3:4b) and performs real package installs in
a disposable virtualenv. Run manually:
    python tests/live_workflow_check.py
Requires the Ollama service on localhost:11434.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=300) as r:
        return json.load(r)


def post(path):
    req = urllib.request.Request(f"{BASE}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
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

    print("Running ONE live workflow (real model + disposable venv install)…")
    start = time.perf_counter()
    inc = post("/workflow")
    wall = round(time.perf_counter() - start, 1)
    print(f"Workflow wall time: {wall}s")
    print("Incident ID:", inc["id"])
    print("Status:", inc["status"])
    print("Alert-to-tested-patch ms:", inc["elapsed_alert_to_tested_patch_ms"])
    print()

    ok = True
    for a in inc["attempts"]:
        print(f"Attempt {a['attempt']} (fault_injection={a['engineer'].get('fault_injection')}):")
        print("  proposal:", a["engineer"]["proposal"])
        print("  installer invoked:", a["verification"].get("installer_invoked"))
        print("  installed:", a["verification"].get("installed_version"))
        print("  pytest rc:", a["verification"].get("pytest_returncode"))
        print("  junit:", json.dumps(a["verification"].get("junit")))
        print("  artifact sha256:", str(a["verification"].get("artifact_sha256"))[:16], "…")
        print("  QA approval:", a["qa"]["approval"])
        print("  QA reason:", a["qa"]["reason"][:200])
        print()

    if inc["status"] != "tested_patch_awaiting_human_approval":
        print("[FAIL] Expected tested_patch_awaiting_human_approval, got", inc["status"])
        ok = False
    else:
        # Verify approval + simulation via the live endpoints.
        ap = post(f"/incident/{inc['id']}/approve")
        print("Approval:", ap.get("approved"), ap.get("error") or "")
        sim = post(f"/incident/{inc['id']}/simulate-deploy")
        print("Simulation:", sim.get("simulated"), sim.get("error") or "")
        exp = get(f"/export/{inc['id']}")
        print("Export disclaimer present:", "evidence_disclaimer" in exp)
        print("Export has human_approval:", "human_approval" in exp["incident"])
        print("Export has simulated_deployment:", "simulated_deployment" in exp["incident"])
        ok = ap.get("approved") is True and sim.get("simulated") is True

    print()
    print("[LIVE PASS]" if ok else "[LIVE FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
