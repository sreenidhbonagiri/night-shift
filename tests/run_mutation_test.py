"""Bounded mutation-rejection scenario (C3).

1. Produce a QA-verified proposal via mock.
2. Record explicit approval.
3. Mutate the approved requirements file.
4. Attempt staging through the real release gate.
5. Confirm rejection before env creation/install/process.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import git_proposal as gp
from app.staging import _release_gate

PRIVATE = Path("/Users/sreenidhbonagiri/night-shift-mel-private/evaluation")


def main():
    print("=== Mutation-rejection integrity check ===")

    # Setup artifact dir
    art_dir = tempfile.mkdtemp(prefix="ns-mut-")
    for f in ("main.py", "test_regression.py", "pytest.ini"):
        (Path(art_dir) / f).write_text("# test\n")
    (Path(art_dir) / "requirements.txt").write_text("jinja2==3.1.2\nMarkupSafe>=2.0\n")
    (Path(art_dir) / "manifest.json").write_text("{}")

    inc_id = "integrity-mut-001"
    prop = gp.create_proposal(inc_id, "3.1.4", art_dir, "sha256mut", {})
    if "error" in prop:
        print(f"Proposal failed: {prop['error']}")
        return

    incident = {
        "id": inc_id,
        "status": "human_approved",
        "human_approval": {
            "approved": True,
            "actor": "human",
            "incident_id": inc_id,
            "patch": {"package": "jinja2", "version": "3.1.4"},
            "artifact_sha256": "sha256mut",
        },
        "verified_patch": {
            "package": "jinja2",
            "version": "3.1.4",
            "artifact_sha256": "sha256mut",
            "artifact_dir": art_dir,
            "manifest": {},
            "git_proposal": prop,
        },
        "attempts": [{
            "qa": {"approval": True},
            "verification": {
                "artifact_sha256": "sha256mut",
                "artifact_dir": art_dir,
                "manifest": {},
                "installed_version": "3.1.4",
                "pytest_returncode": 0,
                "junit": {"tests": {}},
                "success": True,
                "installer_invoked": True,
                "requirements_validated": True,
            },
        }],
        "staging_release": {},
    }

    # Step 1-2: proposal + approval already set up above.

    # Step 3: mutate
    malicious = gp.mutate_requirements_for_test(prop)
    print(f"Mutated requirements to: {malicious.strip()[:60]}...")

    # Step 4: attempt staging
    gate_result = _release_gate(incident)

    # Step 5: confirm rejection
    rejected = gate_result is not None
    print(f"\nRelease gate result: {'REJECTED' if rejected else 'PASSED (unexpected!)'}")
    if rejected:
        print(f"Reason: {gate_result}")
    else:
        print("ERROR: mutation was NOT detected by the release gate!")

    # Save report
    PRIVATE.mkdir(parents=True, exist_ok=True)
    report = {
        "scenario": "mutation_rejection_c3",
        "incident_id": inc_id,
        "mutation_applied": True,
        "release_rejected": rejected,
        "rejection_reason": gate_result if rejected else None,
        "proposal_commit": prop.get("proposal_commit"),
        "branch": prop.get("branch"),
    }
    (PRIVATE / "mutation-rejection-report.json").write_text(json.dumps(report, indent=2))
    print(f"\nReport saved to {PRIVATE / 'mutation-rejection-report.json'}")

    # Cleanup
    shutil.rmtree(art_dir, ignore_errors=True)
    repo = gp._GIT_FIXTURE_ROOT / inc_id
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    main()
