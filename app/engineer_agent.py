"""Night Shift Engineer agent: proposes constrained Jinja2 upgrades."""
from datetime import datetime, timezone
from typing import Any, Optional

from app.ollama_client import call_model
from app.isolated_runner import ALLOWED_VERSIONS

# Normal remediation choices. 3.1.3 is deliberately excluded: it fixes
# GHSA-h5c8-rqwp-cp95 but not GHSA-h75v-3vvj-5mfj, so it cannot satisfy the
# current policy (MIN_PATCHED_VERSION = 3.1.4).
ALLOWED_JINJA2_VERSIONS = set(ALLOWED_VERSIONS)


def propose_upgrade(advisory_context: str, previous_rejection: Optional[dict] = None) -> dict:
    system_prompt = (
        "You are the Night Shift Engineer agent. "
        "Propose a Jinja2 dependency upgrade to address the advisory. "
        f"You may ONLY choose from: {sorted(ALLOWED_JINJA2_VERSIONS)}. "
        "Return ONLY JSON: {\"package\":\"jinja2\", \"version\":\"<one allowed version, e.g. 3.1.4>\", \"reason\":\"one sentence\"}. "
        "The version must be an actual allowed version string, not a placeholder. "
        "Never include shell commands or code."
    )

    user_prompt = f"Advisory context:\n{advisory_context}"
    if previous_rejection:
        # Rejection feedback uses the CURRENT evidence fields:
        # pytest_returncode, junit (structured), deterministic_checks.
        user_prompt += (
            f"\n\nYour previous proposal ({previous_rejection.get('proposal', {})}) was rejected."
            f"\nRejection evidence:"
            f"\n  QA reason: {previous_rejection.get('reason', 'unknown')}"
            f"\n  Installed version: {previous_rejection.get('installed_version', 'N/A')}"
            f"\n  pytest return code: {previous_rejection.get('pytest_returncode', 'N/A')}"
            f"\n  JUnit structured results: {previous_rejection.get('junit', 'N/A')}"
            f"\n  Deterministic checks: {previous_rejection.get('deterministic_checks', 'N/A')}"
            "\nPropose a different allowed version that addresses the rejection evidence."
        )

    result = call_model(system_prompt, user_prompt)
    ts = datetime.now(timezone.utc).isoformat()

    if not result["success"]:
        return {
            "proposal": None,
            "reason": f"Model error: {result['error']}",
            "model": "qwen3:4b",
            "elapsed_ms": result["elapsed_ms"],
            "utc_timestamp": ts,
            "error": result["error"],
        }

    parsed = result["response"]
    package = str(parsed.get("package", "")).lower()
    version = str(parsed.get("version", "")).strip()
    reason = str(parsed.get("reason", "")).strip()

    if package != "jinja2" or version not in ALLOWED_JINJA2_VERSIONS or not reason:
        return {
            "proposal": {"package": package, "version": version, "reason": reason},
            "reason": "Proposal failed allowlist validation; rejected before any install.",
            "model": "qwen3:4b",
            "elapsed_ms": result["elapsed_ms"],
            "utc_timestamp": ts,
            "error": "Allowlist validation failed",
            "raw": result["raw"],
        }

    return {
        "proposal": {"package": package, "version": version, "reason": reason},
        "reason": reason,
        "model": "qwen3:4b",
        "elapsed_ms": result["elapsed_ms"],
        "utc_timestamp": ts,
        "error": None,
        "raw": result["raw"],
    }
