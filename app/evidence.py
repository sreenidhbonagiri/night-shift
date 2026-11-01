"""Shared deterministic evidence evaluator.

One evaluator is used at all three boundaries — QA, orchestration, and
approval — so no boundary can succeed on missing, partial, or forged checks.
Checks are always RECOMPUTED from the raw execution evidence; a QA-provided
checks map is never trusted. Strict types are enforced throughout.
"""
from typing import Any

from app.isolated_runner import REQUIRED_TESTS

MIN_PATCHED_VERSION = "3.1.4"


def _parse_version(v) -> tuple:
    if not isinstance(v, str):
        return ()
    parts = []
    for x in v.split("."):
        if not x.isdigit():
            return ()
        parts.append(int(x))
    return tuple(parts)


def _is_strict_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def evaluate(verification: Any) -> dict:
    """Recompute all deterministic checks from execution evidence.

    Strict typing: bools are not ints, missing/partial/None evidence never
    establishes success. Returns a full checks dict; missing evidence keys
    yield False for the corresponding checks and are flagged.
    """
    checks: dict = {}
    if not isinstance(verification, dict):
        return {k: False for k in (
            "evidence_complete", "install_ok", "version_inspection_ok",
            "version_matches_pin", "version_satisfies_policy",
            "pytest_returncode_zero", "required_tests_all_passed",
            "no_failures", "no_errors", "no_skipped")}

    # Evidence completeness with strict types.
    installed = verification.get("installed_version")
    proposed = verification.get("proposed_version")
    rc = verification.get("pytest_returncode")
    junit = verification.get("junit")

    complete = (
        isinstance(installed, str) and bool(installed)
        and isinstance(proposed, str) and bool(proposed)
        and _is_strict_int(rc)
        and isinstance(junit, dict)
        and isinstance(junit.get("tests"), dict)
        and isinstance(verification.get("install_ok"), bool)
        and isinstance(verification.get("version_inspection_ok"), bool)
    )
    checks["evidence_complete"] = complete

    checks["install_ok"] = verification.get("install_ok") is True
    checks["version_inspection_ok"] = verification.get("version_inspection_ok") is True

    checks["version_matches_pin"] = (
        isinstance(installed, str) and isinstance(proposed, str)
        and installed == proposed
    )
    installed_tuple = _parse_version(installed)
    checks["version_satisfies_policy"] = (
        bool(installed_tuple)
        and installed_tuple >= _parse_version(MIN_PATCHED_VERSION)
    )

    checks["pytest_returncode_zero"] = _is_strict_int(rc) and rc == 0

    if isinstance(junit, dict) and isinstance(junit.get("tests"), dict) and not junit.get("parse_error"):
        statuses = junit["tests"]
        checks["required_tests_all_passed"] = all(
            statuses.get(t) == "passed" for t in REQUIRED_TESTS
        )
        checks["no_failures"] = junit.get("failures") == 0
        checks["no_errors"] = junit.get("errors") == 0
        checks["no_skips"] = junit.get("skipped") == 0
    else:
        checks["required_tests_all_passed"] = False
        checks["no_failures"] = False
        checks["no_errors"] = False
        checks["no_skips"] = False

    return checks


def evidence_ok(verification: Any) -> bool:
    """True only when every recomputed check passes."""
    return all(evaluate(verification).values())


def failed_check_names(verification: Any) -> list:
    return sorted(k for k, v in evaluate(verification).items() if not v)
