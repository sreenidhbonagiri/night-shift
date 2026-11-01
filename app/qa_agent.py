"""Night Shift QA agent: verifies installed version and regression evidence.

QA is independent: it receives only the structured execution evidence from the
isolated runner, never the Engineer's conclusion. All deterministic checks are
RECOMPUTED by the shared evaluator in app/evidence.py — a checks map provided
inside the verification dict is never trusted.

Deterministic checks veto any model approval when evidence fails. Model
approval must be a strict JSON boolean; the string "false" (or any other
truthy non-boolean) is treated as malformed, never as approval.
"""
from datetime import datetime, timezone
from typing import Any

from app.ollama_client import call_model
from app import evidence as evidence_mod


def verify(verification: dict, model_fn=call_model) -> dict:
    """Run QA verification over structured execution evidence.

    verification must contain: proposed_version, installed_version,
    pytest_returncode, junit (structured), install_ok, version_inspection_ok.
    """
    # Recompute checks from execution evidence — never trust a provided map.
    checks = evidence_mod.evaluate(verification)
    failed = evidence_mod.failed_check_names(verification)

    deterministic_veto = None
    if failed:
        deterministic_veto = "Deterministic evidence checks failed: " + ", ".join(failed)

    system_prompt = (
        "You are the Night Shift QA agent. "
        "You are shown structured evidence from an isolated test environment. "
        "Return ONLY JSON: {\"approval\": <true or false, JSON boolean>, \"reason\":\"one sentence\"}. "
        "Approve ONLY if the installed version matches the proposed pin, satisfies policy, "
        "and all required regression tests passed with zero failures, errors, or skips. "
        "Do not trust Engineer's conclusion; use the evidence."
    )

    user_prompt = (
        f"Proposed version: {verification.get('proposed_version')}\n"
        f"Installed version: {verification.get('installed_version')}\n"
        f"pytest return code: {verification.get('pytest_returncode')}\n"
        f"JUnit structured results: {verification.get('junit')}\n"
        f"Deterministic checks: {checks}"
    )

    result = model_fn(system_prompt, user_prompt)
    ts = datetime.now(timezone.utc).isoformat()

    base = {
        "utc_timestamp": ts,
        "installed_version": verification.get("installed_version"),
        "proposed_version": verification.get("proposed_version"),
        "deterministic_checks": checks,
        "deterministic_veto": deterministic_veto,
        "model": "qwen3:4b",
    }

    if not result["success"]:
        return dict(base, approval=False,
                    reason=f"Model error: {result['error']}",
                    elapsed_ms=result["elapsed_ms"], error=result["error"], raw=result["raw"])

    parsed = result["response"]
    model_approval = parsed.get("approval")
    model_reason = str(parsed.get("reason", "")).strip()

    # Strict boolean: only Python True/False accepted.
    if model_approval is not True and model_approval is not False:
        return dict(base, approval=False,
                    reason=f"Malformed model output: 'approval' must be a JSON boolean, got {model_approval!r}.",
                    elapsed_ms=result["elapsed_ms"], error="Malformed model output", raw=result["raw"])

    if not model_reason:
        return dict(base, approval=False,
                    reason="Malformed model output: empty reason.",
                    elapsed_ms=result["elapsed_ms"], error="Malformed model output", raw=result["raw"])

    # Deterministic veto overrides model approval.
    if deterministic_veto:
        return dict(base, approval=False,
                    reason=f"{deterministic_veto} (model said: {model_reason})",
                    elapsed_ms=result["elapsed_ms"], model_approval=model_approval,
                    model_reason=model_reason, error=None, raw=result["raw"])

    return dict(base, approval=model_approval,
                reason=model_reason,
                elapsed_ms=result["elapsed_ms"], model_approval=model_approval,
                model_reason=model_reason, error=None, raw=result["raw"])
