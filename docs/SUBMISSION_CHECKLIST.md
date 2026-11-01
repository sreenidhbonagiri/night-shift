# Submission checklist

Status legend: ☐ not done · ◐ in progress · ☑ done. Nothing here has been
published; "prepare" items are complete only to the point of being ready.

## Public repository
- ☑ Code, README, PROVENANCE, work log committed locally (`git log`
  verifies Mel-authored commits: `8c5dc83` → `dd6f093` → `f2366be` →
  `50cf43b` → `c5b3c35` → this checkpoint).
- ☑ `.gitignore` excludes `.venv/`, secrets patterns (`*.pem`, `*.key`,
  `.env`), raw session records (`*.events.jsonl`,
  `mel-conversation*.json*`, `events/`), and runtime artifacts
  (`artifacts/`, `staging/`, `*.log`).
- ☑ Secrets/private-record audit of tracked files performed (see
  `docs/SUBMISSION_AUDIT.md`): no keys, tokens, environments, or session
  records tracked; only personal absolute paths in `MEL_WORK_LOG.md` (the
  work log is a submission artifact and intentionally names the working
  directory; redact or accept before publishing).
- ☐ Choose final repo visibility/name; push (NOT DONE — deliberately held
  until the submitter approves publishing).
- ☐ License file decision (none present).
- ☐ Review PROVENANCE.md wording against what will be public.

## Demo video
- ☑ Outline written (`docs/DEMO_OUTLINE.md`) using the actual measured
  results (42.2 s wall, 40,345.29 ms alert-to-tested-patch, attempt 1
  fault-injected rc=1/2 failures, attempt 2 3.1.4 4/4, staging health
  `jinja2 3.1.4`/`MarkupSafe 2.1.5`, smoke all-true).
- ☐ Record the three-minute video following the outline.
- ☐ On-screen labels present for fault injection and historical
  remediation.

## Mel-session video / export
- ☐ Screen-record a Mel session (or export the conversation) demonstrating:
  the interrupted-state recovery, the mock test runs, and the bounded live
  recovery. (Raw conversation files are private per PROVENANCE.md; any
  shared export must be reviewed and sanitized first.)
- ☐ If sharing the raw session export, confirm it contains no other
  projects' paths or unrelated personal data.

## Work log
- ☑ `MEL_WORK_LOG.md` maintained through checkpoint 8 with actual results,
  root causes, and honest failure records (including the grounding
  handoffs that are part of the design).

## Test proof
- ☑ Mock suites green, commands recorded in README and the work log:
  - `tests/test_grounded_live.py` — 26 passed
  - `tests/test_mock_acceptance.py` — 64 passed
  - `tests/test_staging.py` — 16 passed
- ☐ Capture a fresh full-suite run output (with timestamps) as submission
  proof on the final code state.

## Live-run proof
- ☑ One bounded live recovery recorded with preserved export (checkpoint 7:
  bad-patch rejection on real JUnit evidence → grounded 3.1.4 → approval →
  local staging health/smoke → stop).
- ☐ Decide whether to include the sanitized example export
  (`docs/example-export.json`) in the public repo (recommended: yes — it
  contains no secrets).

## Customer quote permission
- ☐ Obtain and record written permission before using any customer quote
  or customer-identifying material. (No quote is used anywhere in this
  repository today; nothing to attribute yet.)
- ☐ If a quote is added later, store the permission reference beside it.

## Sunday curveball
- ☑ **Correction (checkpoint 10):** the "Sunday curveball" is the
  **organizer's actual new request**, not our own failure-path rehearsal.
  This item stays **pending until the request is received and implemented**;
  do not pre-empt it. (Rehearsing the fail-closed paths remains useful demo
  preparation, recorded under Demo video above, but it is not the
  curveball itself.)
- ☐ Await the organizer's actual Sunday request; scope, implement, verify,
  and record it when it arrives. Until then this checklist item is open.

## Pre-publish final pass
- ☐ Run the secret/path audit script once more on the final tree.
- ☑ No publishing or deployment performed in preparing these materials.
