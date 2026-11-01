"""Night Shift Engineer agent: grounded interpretation of orchestrator-collected evidence.

Evidence collection is deterministic and orchestrator-selected for this
single known fixture: Python itself reads the affected dependency
declaration and the relevant application source through the existing
restricted read-only tools (app.eng_tools). The actual executed reads and
their numbered contents are recorded. These are NOT model-selected tool
calls.

The model's single role is interpretation: it receives the recorded
evidence, assesses applicability and uncertainty, returns verified
citations, and proposes an allowlisted remediation version. Citations are
validated against the recorded numbered contents — model-generated claims
are never accepted as evidence, and no findings, citations, or remediation
version are hardcoded.

A mandatory grounding gate must pass before a proposal is accepted. If it
cannot be satisfied after one bounded retry, the result is proposal=None +
needs_human_review=True — no installation, approval, or release follows.
"""
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.ollama_client import call_model
from app.isolated_runner import ALLOWED_VERSIONS
from app import eng_tools

ALLOWED_JINJA2_VERSIONS = ALLOWED_VERSIONS

FIXTURE_DECLARED_VERSION = "3.1.2"

MAX_GROUNDING_RETRIES = 1

# Orchestrator-selected evidence targets for this single known fixture.
# The selection is transparent and deterministic; only the file names are
# fixed — findings, citations, and the remediation version are not.
EVIDENCE_TARGETS = (
    ("requirements-affected.txt", "affected dependency declaration"),
    ("main.py", "relevant application source"),
)

EVIDENCE_SELECTED_BY = "orchestrator (deterministic evidence collection, not model-selected tool calls)"


def _collect_evidence() -> tuple[list, list]:
    """Deterministically read the evidence targets with the restricted tools.

    Returns (reads, problems). Each read records the executed tool call,
    its actual result, and — on success — the numbered contents. Problems
    list any evidence-read failures (missing file, tool error).
    """
    reads = []
    problems = []
    for name, purpose in EVIDENCE_TARGETS:
        result = eng_tools.read_file(name)
        entry = {
            "tool": "read_file",
            "args": {"path": name},
            "purpose": purpose,
            "selected_by": EVIDENCE_SELECTED_BY,
            "result": result,
        }
        if isinstance(result, dict) and "content" in result:
            lines = result["content"].splitlines()
            entry["numbered_content"] = {
                str(i): line for i, line in enumerate(lines, start=1)
            }
            reads.append(entry)
        else:
            error = result.get("error") if isinstance(result, dict) else "tool failure"
            problems.append(f"evidence read of {name!r} ({purpose}) failed: {error}")
            reads.append(entry)
    return reads, problems


def _reads_summary(reads: list) -> str:
    """Human-readable summary of the executed reads for the model prompt."""
    parts = []
    for entry in reads:
        name = entry["args"]["path"]
        purpose = entry["purpose"]
        result = entry["result"]
        if "content" in result:
            numbered = "\n".join(
                f"{i:>4} | {line}" for i, line in entry["numbered_content"].items()
            )
            parts.append(
                f"read_file(path='{name}') — {purpose} — executed by the "
                f"orchestrator. Contents (line numbers shown):\n"
                f"{numbered}"
            )
        else:
            parts.append(
                f"read_file(path='{name}') — {purpose} — FAILED: "
                f"{result.get('error', 'unknown error')}"
            )
    return "\n\n".join(parts)


def _citation_check(evidence: list, reads: list) -> tuple[list, list]:
    """Verify that each citation's (file, line) exists in the recorded reads.

    Returns (citation_problems, verified_evidence).
    A citation is verified when its file was successfully read and its line
    number exists in the recorded numbered contents.
    """
    file_lines = {}
    for entry in reads:
        result = entry["result"]
        if isinstance(result, dict) and "content" in result:
            file_lines[result["file"]] = entry["numbered_content"]

    problems = []
    verified = []
    for i, ev in enumerate(evidence):
        if not isinstance(ev, dict):
            problems.append(f"evidence[{i}] is not an object")
            continue
        fname = ev.get("file")
        line = ev.get("line")
        if not isinstance(fname, str) or fname not in file_lines:
            problems.append(f"evidence[{i}] cites file '{fname}' which was not read")
            continue
        if not isinstance(line, int) or str(line) not in file_lines[fname]:
            problems.append(
                f"evidence[{i}] cites {fname}:{line} which is out of range "
                f"(file has {len(file_lines[fname])} lines)")
            continue
        verified.append(ev)

    return problems, verified


def _grounding_problems(collection_problems: list, reads: list, files_read: list,
                        declared_version: Optional[str],
                        evidence: list, verified: list,
                        citation_problems: Optional[list] = None) -> list:
    """Mandatory grounding gate. Returns a list of problems (empty = pass)."""
    problems = list(collection_problems)

    # Must have read at least one .py source file.
    if not any(f.endswith(".py") for f in files_read):
        problems.append(
            "no application source file (.py) was read")

    # Must have read a dependency declaration file.
    if not any(f.startswith("requirements") for f in files_read):
        problems.append(
            "no dependency declaration file was read")

    # Declared version must match the fixture's actual pin.
    if not isinstance(declared_version, str) or not declared_version.strip():
        problems.append("no declared version was provided")
    elif declared_version.strip() != FIXTURE_DECLARED_VERSION:
        problems.append(
            f"declared version {declared_version!r} does not match the "
            f"fixture pin {FIXTURE_DECLARED_VERSION!r}")

    # Must have non-empty evidence.
    if not isinstance(evidence, list) or not evidence:
        problems.append("no evidence citations were provided")

    # ALL citations must be verified: any unverified citation fails
    # grounding, even alongside valid ones. (Regression: previously only
    # an empty verified list failed.)
    if citation_problems:
        problems.append(
            f"{len(citation_problems)} unverified citation(s) present: "
            + "; ".join(citation_problems))

    # All citations must be verified (empty verified list with non-empty
    # evidence is already covered by citation_problems above, but keep the
    # explicit check for the evidence-present-but-none-verified case).
    if evidence and not verified and not citation_problems:
        problems.append("no evidence citations were verified against the recorded reads")

    # Must have verified evidence from application source.
    if verified and not any(
            (ev.get("file") or "").endswith(".py") for ev in verified):
        problems.append(
            "no verified citation from application source code")

    # Must have verified evidence from dependency declaration.
    if verified and not any(
            (ev.get("file") or "").startswith("requirements") for ev in verified):
        problems.append(
            "no verified citation from the dependency declaration file")

    return problems


def _interpret(advisory_context: str, reads: list, collection_problems: list,
               previous_rejection: Optional[dict],
               model_fn=None) -> dict:
    """Run the model's interpretation of the collected evidence.

    Returns the standard engineer result dict.
    """
    _call = model_fn or call_model

    files_read = [
        entry["result"]["file"] for entry in reads
        if isinstance(entry["result"], dict) and "file" in entry["result"]
    ]

    system_prompt = (
        "You are the Night Shift Engineer agent. "
        "You are shown evidence collected by the orchestrator from the fixture "
        "application using restricted read-only tools: the affected dependency "
        "declaration and the relevant application source, with line numbers. "
        "Interpret this evidence and propose a Jinja2 dependency upgrade."
        f"\n\nYou may ONLY propose one of these versions: {sorted(ALLOWED_JINJA2_VERSIONS)}."
        "\n\nReturn ONLY JSON with this structure:"
        '\n  {"declared_version": "<the Jinja2 version pinned in the dependency declaration>",'
        '\n   "applicability": "one sentence on whether the advisory applies to this application, based on the evidence",'
        '\n   "uncertainty": "one sentence on what remains uncertain",'
        '\n   "evidence": [{"file": "main.py", "line": 10, "finding": "desc"}],'
        '\n   "proposal": {"package": "jinja2", "version": "3.1.4"},'
        '\n   "rationale": "one sentence forward-looking rationale, NOT observed test results"}'
        "\n\nRules:"
        "\n- declared_version must be the version actually pinned in the dependency"
        " declaration you were shown — this is the DECLARED version, distinct from"
        " any installed version."
        "\n- Every evidence citation must reference a file and line number that"
        " exists in the evidence you were shown. Citations are verified against"
        " the recorded reads; anything else is rejected."
        "\n- Include at least one citation from the application source and at"
        " least one from the dependency declaration."
        "\n- Never present future test results as observed."
    )

    user_prompt = f"Advisory context:\n{advisory_context}\n\n"
    if previous_rejection:
        user_prompt += (
            f"Your previous proposal ({previous_rejection.get('proposal', {})}) was rejected."
            f"\nRejection evidence:"
            f"\n  QA reason: {previous_rejection.get('reason', 'unknown')}"
            f"\n  Installed version: {previous_rejection.get('installed_version', 'N/A')}"
            f"\n  pytest return code: {previous_rejection.get('pytest_returncode', 'N/A')}"
            f"\n  JUnit structured results: {previous_rejection.get('junit', 'N/A')}"
            f"\n  Deterministic checks: {previous_rejection.get('deterministic_checks', 'N/A')}"
            "\nPropose a different allowed version that addresses the rejection evidence."
            "\n\n"
        )
    user_prompt += (
        f"Evidence collected by the orchestrator (executed reads with numbered contents):\n\n"
        f"{_reads_summary(reads)}\n\n"
        "Based on the evidence above, return your declared_version, applicability, "
        "uncertainty, evidence citations, proposal, and rationale."
    )

    # timeout=None lets the shared client apply its configured default
    # (OLLAMA_TIMEOUT env var), so the Engineer cannot silently override
    # the deployment's model-timeout setting.
    result = _call(system_prompt, user_prompt, expect_json=True, timeout=None)
    ts = datetime.now(timezone.utc).isoformat()
    elapsed_ms = result.get("elapsed_ms", 0.0)
    raw = result.get("raw", "")

    if not result["success"]:
        return {
            "proposal": None,
            "reason": f"Model error during interpretation: {result['error']}",
            "model": "qwen3:4b",
            "elapsed_ms": elapsed_ms,
            "utc_timestamp": ts,
            "error": result["error"],
            "fault_injection": False,
            "raw": raw,
            "investigation": {
                "tool_calls": reads,
                "files_read": files_read,
                "evidence": [],
                "declared_version": None,
                "fixture_declared_version": FIXTURE_DECLARED_VERSION,
                "citation_problems": ["model error — no interpretation performed"],
                "grounding_problems": [f"model error: {result['error']}"],
                "needs_human_review": True,
                "evidence_collection": {
                    "selected_by": EVIDENCE_SELECTED_BY,
                    "targets": [t[0] for t in EVIDENCE_TARGETS],
                    "problems": collection_problems,
                },
            },
        }

    parsed = result["response"]
    if not isinstance(parsed, dict):
        return {
            "proposal": None,
            "reason": "Malformed model output: response was not a JSON object.",
            "model": "qwen3:4b",
            "elapsed_ms": elapsed_ms,
            "utc_timestamp": ts,
            "error": "Malformed model output",
            "fault_injection": False,
            "raw": raw,
            "investigation": {
                "tool_calls": reads,
                "files_read": files_read,
                "evidence": [],
                "declared_version": None,
                "fixture_declared_version": FIXTURE_DECLARED_VERSION,
                "citation_problems": ["malformed model output — no interpretation"],
                "grounding_problems": ["malformed model output — grounding not possible"],
                "needs_human_review": True,
                "evidence_collection": {
                    "selected_by": EVIDENCE_SELECTED_BY,
                    "targets": [t[0] for t in EVIDENCE_TARGETS],
                    "problems": collection_problems,
                },
            },
        }

    # Extract interpretation fields.
    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []
    declared_version = parsed.get("declared_version")
    if not isinstance(declared_version, str):
        declared_version = None
    else:
        declared_version = declared_version.strip()

    # Verify citations against the recorded reads.
    citation_problems, verified = _citation_check(evidence, reads)

    # Check grounding.
    grounding_problems = _grounding_problems(
        collection_problems, reads, files_read, declared_version, evidence,
        verified, citation_problems)

    investigation = {
        "tool_calls": reads,
        "files_read": files_read,
        "evidence": evidence,
        "declared_version": declared_version,
        "fixture_declared_version": FIXTURE_DECLARED_VERSION,
        "citation_problems": citation_problems,
        "grounding_problems": grounding_problems if grounding_problems else None,
        "needs_human_review": bool(grounding_problems),
        "applicability": parsed.get("applicability"),
        "uncertainty": parsed.get("uncertainty"),
        "evidence_collection": {
            "selected_by": EVIDENCE_SELECTED_BY,
            "targets": [t[0] for t in EVIDENCE_TARGETS],
            "problems": collection_problems,
        },
    }

    # If grounded, extract the proposal. If not, proposal stays None.
    proposal = None
    reason = "Grounding failed after the bounded retry — handed to human review."
    error = None

    if not grounding_problems:
        pkg = str(parsed.get("proposal", {}).get("package", "")).lower()
        ver = str(parsed.get("proposal", {}).get("version", "")).strip()
        rationale = str(parsed.get("rationale", "")).strip()

        if pkg != "jinja2" or ver not in ALLOWED_JINJA2_VERSIONS or not rationale:
            # Grounding passed but proposal failed allowlist — not a grounding
            # failure, but the proposal is invalid.
            proposal = {"package": pkg, "version": ver, "reason": rationale}
            reason = ("Proposal failed allowlist validation; rejected before "
                      "any install.")
            error = "Allowlist validation failed"
        else:
            proposal = {"package": pkg, "version": ver, "reason": rationale}
            reason = rationale
            error = None
    else:
        error = "Grounding gate not satisfied"

    return {
        "proposal": proposal,
        "reason": reason,
        "model": "qwen3:4b",
        "elapsed_ms": elapsed_ms,
        "utc_timestamp": ts,
        "error": error,
        "fault_injection": False,
        "raw": raw,
        "investigation": investigation,
    }


def propose_upgrade(advisory_context: str,
                    previous_rejection: Optional[dict] = None,
                    model_fn=None) -> dict:
    """Propose a Jinja2 upgrade from orchestrator-collected, model-interpreted evidence.

    On grounding failure after the bounded retry, returns proposal=None +
    needs_human_review=True.
    """
    # Deterministic evidence collection (orchestrator-selected).
    reads, collection_problems = _collect_evidence()

    # First interpretation attempt.
    out = _interpret(advisory_context, reads, collection_problems,
                     previous_rejection, model_fn)

    if out["investigation"].get("needs_human_review"):
        # One bounded retry: re-collect (transparent, deterministic) and
        # re-interpret.
        reads, collection_problems = _collect_evidence()
        retry_out = _interpret(advisory_context, reads, collection_problems,
                               previous_rejection, model_fn)
        retry_out["investigation"]["retry"] = True
        retry_out["investigation"]["first_attempt_problems"] = (
            out["investigation"].get("grounding_problems"))
        if not retry_out["investigation"].get("needs_human_review"):
            return retry_out
        # Still failing after retry — hand off to human.
        retry_out["proposal"] = None
        retry_out["reason"] = (
            "Grounding failed after the bounded retry — handed to human review. "
            "No installation, approval, or release followed.")
        retry_out["error"] = "Grounding gate not satisfied after retry"
        retry_out["investigation"]["needs_human_review"] = True
        return retry_out

    return out
