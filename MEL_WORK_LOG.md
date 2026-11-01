# Night Shift — Mel Work Log

## Session
- Date: 2026-11-01
- Working directory: `<repo-root>/night-shift-mel`

## Staging checkpoint — real human-approved local staging (Mel-authored)

Implemented from the recovered baseline entirely in this Mel session. No
source or patches copied from the original night-shift project. The existing
64-test integrity/QA suite passes unchanged.

### New module `app/staging.py`
Real local staging, kept strictly separate from
`approval.simulate_deploy` (which remains simulation-only).

Release gate (all before any process starts, all fail closed):
- human approval record present and incident status `human_approved`
- no staging process already running; no prior recorded release
- the affected fault-injection version 3.1.2 can NEVER be released — even
  with a forged approval record
- approval binding must match the incident's verified patch exactly
  (package, version, artifact_sha256)
- execution-evidence checks recomputed at the release boundary
- on-disk artifact manifest recomputed and compared with the verified
  manifest (again before copying)

Execution:
- separate disposable venv built ONLY from the artifact snapshot's own
  proposed requirements (application pins: jinja2 + MarkupSafe==2.1.5;
  pytest is never shipped)
- server runs a fixed trusted script (`server.py`, generated with fixed
  content) with fixed arguments; binds 127.0.0.1 only
- health gate: JSON with status "ok" AND installed jinja2 == approved pin
  AND MarkupSafe == 2.1.5 exactly
- smoke gate: XSS escaping + CVE-2024-22195 + CVE-2024-34064 checks
- fail closed on malformed responses, wrong versions, startup exit, or
  timeout — process stopped and staging directory removed
- Popen objects live in a module registry keyed by incident id, never in
  incident records (which stay JSON-serializable)
- log file handle closed on Popen failure

Lifecycle: `release`, `stop` (terminate→kill, remove staging dir), `status`.
Release events (`release_succeeded`, `release_failed`, `stop`) and the
`staging_release` record are preserved in the incident and its JSON export.

### API (`app/main.py`)
- `POST /incident/{id}/staging-release`
- `POST /incident/{id}/staging-stop`
- `GET  /incident/{id}/staging-status`
All under the existing per-incident locks. Interface otherwise unchanged
(the embedded dashboard does not yet expose staging; minimal for now).

### Mock tests (`tests/test_staging.py`, 16 tests)
Approval-required; forged 3.1.2 release rejected; approval version
mismatch rejected; mutated artifact rejected; failed evidence rejected;
duplicate prevention; wrong jinja2/MarkupSafe health versions fail closed;
bad health status and non-dict (malformed) health payloads fail closed;
smoke failure cleans up; startup exit cleans up; env build failure cleans
up; success path records + stop + no double-stop + JSON-serializable
incident + export events; gate failures recorded as `release_failed` events.

### Verification results (actual)

Mock suites:
```
python tests/test_staging.py           → Ran 16 tests, OK
python tests/test_mock_acceptance.py   → Ran 64 tests, OK
```

One bounded live check (`tests/live_staging_check.py`), localhost only:
- Workflow: 34.1s; attempt 1 fault-injected 3.1.2 (rc 1, 2 failures,
  QA False), attempt 2 model 3.1.4 (rc 0, 0 failures, QA True).
- Approval: True.
- Release: True — version 3.1.4, port 64975, bound to 127.0.0.1;
  health {"status":"ok","jinja2":"3.1.4","MarkupSafe":"2.1.5"};
  smoke all three checks true.
- Status: running, process_alive true.
- Duplicate release: rejected ("A staging process is already running").
- Direct external /health and /smoke on the staging port: both ok.
- Stop: True; port closed after stop confirmed.
- Export: staging events [release_succeeded, release_failed, stop]
  present; staging_release record present.

```
[LIVE STAGING PASS]
```

Server stopped after the check.

## Prior checkpoint (recovery)

## Recovery checkpoint (Mel-only reconstruction + integrity repair)

This repository was reconstructed in Mel from the unfinished Mel-only state
at the pre-cutoff boundary (session event seq 1746, 2026-10-31 20:10:27).
The original `<repo-root>/night-shift` directory is preserved
unchanged as a reference; nothing was copied from it. Reconstruction used
only verified successful pre-cutoff Mel session operations (write_file /
edit_file replays plus two terminal `sed` substitutions reproduced as direct
string replacements; no historical shell commands were executed). See
PROVENANCE.md.

### Baseline state as recovered (commit 8c5dc83)
- Known defects preserved intentionally: `TestWorkflowScenarios` defined
  twice in tests/test_mock_acceptance.py (the second shadowed the first, so
  only 60 of 70 defined tests ran), and one assertion used the typo'd status
  `tested_patch_awaiting_human_ability`.
- First run after reconstruction: `Ran 60 tests ... FAILED (failures=6)`.

### Repairs (all authored in Mel, in this session)
1. `app/isolated_runner.py`:
   - Application supporting pin is now `MarkupSafe==2.1.5` ONLY; the
     verification tool pin (`pytest==8.3.3`) is separate
     (`VERIFICATION_TOOL_PIN`) and installed separately, never part of the
     application's requirements.
   - `_parse_junit` aggregate counts must be non-negative integer TEXT
     (rejects `true`, `1.5`, `-1`, missing attributes as parse errors) and
     must match the derived per-testcase counts.
   - `persist_artifact` forwards `fault_injection` into
     `validate_requirements` so the orchestrator's trusted labeled scenario
     can persist 3.1.2; model attempts still cannot.
2. `app/evidence.py`: `no_failures`/`no_errors`/`no_skips` are derived from
   per-test statuses — any failed/errored/skipped testcase blocks success,
   including tests outside the required four.
3. `tests/test_mock_acceptance.py`:
   - Removed the duplicate `TestWorkflowScenarios` class (which had shadowed
     the fuller first definition and hid 4 passing tests plus the failing
     string-"false" case).
   - Fixed `tested_patch_awaiting_human_ability` →
     `tested_patch_awaiting_human_approval`.
   - `test_model_false_string_rejected` now supplies proposals for both
     attempts (the malformed-QA rejection triggers a second engineer call).

### Verification results (actual)

Mock suite after repairs:
```
.venv/bin/python tests/test_mock_acceptance.py
Ran 64 tests in 0.327s
OK
```

One bounded live recovery (localhost 127.0.0.1:8000, real Ollama
qwen3:4b, real disposable-venv installs):
- Attempt 1 (fault injection, jinja2 3.1.2): installer invoked, installed
  3.1.2, pytest rc 1, 2 junit failures
  (`test_xmlattr_rejects_space_in_key`, `test_xmlattr_rejects_slash_in_key`),
  QA approval False — rejected on real evidence.
- Attempt 2 (model, jinja2 3.1.4): installed 3.1.4, pytest rc 0, all 4
  required tests passed, QA approval True.
- Human approval: True. Simulated deployment: True. Export contains the
  disclaimer, human_approval, and simulated_deployment.

```
[LIVE PASS]
```

Server stopped after the check. The original night-shift directory was not
modified (no git repo there; no writes performed).

### Notes
- Raw session records remain private, outside this repository.
- Git records changes, not authorship; PROVENANCE.md documents the
  reconstruction method and the cutoff evidence.
- Dashboard/staging/grounded-investigation/live-progress reconstruction is
  deliberately out of scope for this checkpoint.
- Below: the historical Mel work log sections recovered with the baseline.

## Session (historical, recovered with the baseline)
- Date: 2026-10-31
- Working directory: `<repo-root>/night-shift`

## Integrity Checkpoint (requirements-boundary, artifact, evidence-gate hardening)

All work in `<repo-root>/night-shift`, localhost-only. No
hosting, publishing, release implementation, or redesign.

### Files changed
- **Rewritten** `app/isolated_runner.py` — installer boundary + persisted artifacts
- **Created** `app/evidence.py` — shared deterministic evidence evaluator
- **Rewritten** `app/qa_agent.py` — uses shared evaluator, never trusts a provided checks map
- **Rewritten** `app/workflow.py` — persists per-incident artifacts, recomputes evidence at orchestration boundary, current rejection fields
- **Rewritten** `app/approval.py` — recomputes artifact manifest from disk at approve + deploy
- **Edited** `app/engineer_agent.py` — 3.1.3 removed; retry prompt uses current fields; prompt placeholder fixed
- **Edited** `app/main.py` — none (export already returns full incident; requirements_diff and manifest ride in verified_patch)
- **Rewritten** `tests/test_mock_acceptance.py` — 52 mock tests
- `tests/live_workflow_check.py` unchanged

### 1. Installer boundary
- `validate_requirements(text, version)` validates the actual requirements
  bytes immediately before installation: exactly the fixed Jinja2 pin plus
  the exact supporting pin `MarkupSafe>=2.0`. Extra lines, URLs, editable
  installs, pip options, comments, `@`/`;`/spaces are rejected BEFORE any
  subprocess starts (mock-verified: mutated file → `requirements_validated:
  False`, `pytest_returncode: None`).
- `validate_version(version, fault_injection)`: normal path allows only
  {3.1.4, 3.1.5, 3.1.6}; the trusted fault-injection exception allows
  EXACTLY `3.1.2` — never arbitrary numeric versions (tested against 3.1.3,
  3.1.4, 9.9.9, injection strings). Model proposals cannot activate it: the
  workflow keys the exception on the orchestrator-generated attempt 1
  scenario only; a forged `fault_injection: True` flag on a model attempt
  still gets allowlist treatment and is blocked (mock-tested).

### 2. Persisted artifact
- `persist_artifact(incident_id, version, fault_injection)` writes
  `artifacts/<incident_id>/` containing `requirements-original.txt`,
  `requirements-proposed.txt`, `requirements.diff`, and the trusted fixture
  application code + regression tests (`app/main.py`,
  `app/test_regression.py`, `app/pytest.ini`).
- `compute_manifest(dir)` computes a canonical manifest of file hashes from
  actual bytes (`{files: {relpath: sha256}, digest}`), excluding
  `manifest.json` itself.
- `verify_patch` executes the SNAPSHOT's app directory, never the mutable
  fixture source.
- Approval and simulated deploy recompute the manifest from disk
  (`recompute_manifest` + `manifest_matches`) and compare per-file hashes and
  the file set — not merely stored hash strings. Missing, extra, or changed
  files fail closed (mock-tested for requirements, app-code, and test-file
  mutation, file deletion, and whole-directory deletion).
- The requirements diff and full manifest are included in the incident
  export under `verified_patch` (live-verified via `/export/<id>`).

### 3. Independent evidence gates
- One shared evaluator (`app/evidence.py`) used at QA, orchestration, and
  approval boundaries. It recomputes 10 checks with strict types (bools are
  not ints; string "0" is not rc 0): evidence_complete, install_ok,
  version_inspection_ok, version_matches_pin, version_satisfies_policy
  (>= 3.1.4), pytest_returncode_zero, required_tests_all_passed, no_failures,
  no_errors, no_skips.
- Missing or partial evidence never establishes success; absent/partial
  QA-provided checks maps with failed execution evidence are ignored and
  recomputed (mock-tested at QA and approval boundaries with forged
  all-true maps).
- Model approval remains an additional veto, never a replacement: the
  deterministic veto overrides any model approval.

### 4. Repair rejection feedback
- The Engineer retry prompt now receives `pytest_returncode`, `junit`
  (structured), and `deterministic_checks` (recomputed). Mock test verifies
  the actual values (rc=1, failures=2, failed check names) reach the second
  Engineer call.
- 3.1.3 removed from normal remediation choices (cannot satisfy
  MIN_PATCHED_VERSION 3.1.4); model proposals of 3.1.3 are boundary-blocked
  (mock-tested).

### 5. Verification (actual results)

Mock suite (no model calls, no package installs):
```
$ python tests/test_mock_acceptance.py
Ran 52 tests in 0.225s
OK
```
Covers: requirements byte-validation (exact pins, extra line, URL,
editable, pip option, wrong pin), verify_patch rejecting modified
requirements before any subprocess, exact 3.1.2 fault-injection exception,
model/forged-flag activation attempts blocked, 3.1.3 blocked, artifact
persistence contents, canonical manifest stability and mutation detection
(app code, tests, requirements, missing file, extra file, missing dir),
JUnit parsing (passed/failed/skipped/errored/missing/duplicate/garbage),
shared-evaluator strict types and fail-closed behavior, forged and partial
checks maps, workflow scenarios (verified, fault-injection rejected,
both-rejected, blocked, rejection-prompt fields), and approval gates
(intact, mutated, missing files/dir, forged/missing checks, status, repeat,
deploy gating, post-approval mutation).

Live bounded verification (one run, real Ollama qwen3:4b + real
disposable-venv installs; `tests/live_workflow_check.py` unchanged):
```
Incident ID: a82676c8-4c4e-4654-bb7f-5ae30d895a31
Status: tested_patch_awaiting_human_approval
Alert-to-tested-patch ms: 32780.07

Attempt 1 (fault_injection=True): jinja2==3.1.2 installed, pytest rc 1,
  junit 2 passed / 2 failed (both CVE regression tests failed),
  QA rejected on deterministic evidence.
Attempt 2 (model): jinja2==3.1.4 installed, pytest rc 0, junit 4/4 passed,
  QA approved.
Approval: True (manifest recheck passed)
Simulation: True (manifest recheck passed at release time)
Export: manifest + requirements diff present, disclaimer present.
[LIVE PASS]
```

### Honest notes / unresolved limitations
- Two earlier live runs FAILED before the pass above: the Engineer model
  returned the literal placeholder "x.y.z" from the prompt's format example.
  Fixed by making the example concrete (`e.g. 3.1.4` + explicit
  "not a placeholder" instruction). One of those runs also executed against
  a stale server process (pre-fix code); the passing run was after a server
  restart with the fixed prompt.
- Two mock tests initially failed due to test-fixture bugs (set slicing,
  mutating shorthand keys instead of the real evidence fields); fixed in the
  tests, not by weakening code.
- Local approval remains UNAUTHENTICATED: any local request can approve.
  This is a known limitation, not a claim of security.
- Incidents and approvals are in-memory only; the service restarts to a
  clean state. Artifacts persist on disk under `artifacts/`.
- The app is NOT ready for public hosting.

- Mel model: auto

## Checkpoint 5 (Cursor review remediation)

This checkpoint fixed root causes found in an independent Cursor review. All
work stayed in `<repo-root>/night-shift`; the service remained
localhost-only; no publishing, deployment, or interface redesign.

### Files rewritten / created / removed
- **Rewritten** `app/isolated_runner.py`
- **Rewritten** `app/qa_agent.py`
- **Rewritten** `app/workflow.py`
- **Rewritten** `app/approval.py`
- **Rewritten** `app/main.py`
- **Created** `tests/test_mock_acceptance.py` (27 mock-based tests, no model
  calls, no package installs)
- **Created** `tests/live_workflow_check.py` (bounded single live run)
- **Removed** `tests/test_checkpoint1.py`, `tests/test_checkpoint2.py`,
  `tests/test_checkpoint3.py`, `tests/test_checkpoint4.py` — these contained
  placeholder checks that printed PASS without exercising behavior (e.g.
  "approval precondition semantics covered by status/incident binding tests").

### Requirement 1 — proposal validation at both boundaries
- `workflow._validate_proposal()` is the orchestration boundary: a proposal
  passes only if `package` equals the fixed constant `REMEDIATION_PACKAGE`
  ("jinja2") and the version matches `_VERSION_RE` (`^\d+\.\d+\.\d+$`) AND is
  in `ALLOWED_JINJA2_VERSIONS`. Model proposals failing this never invoke the
  runner (verified by mock tests: `runner.assert_not_called()`).
- `isolated_runner.build_artifact()` re-validates the version with the same
  regex before constructing the artifact; `verify_patch()` installs from the
  artifact's requirements file only.
- Version strings are used solely as pip CLI arguments. Version inspection
  runs `from importlib.metadata import version; print(version('jinja2'))`
  where "jinja2" is a fixed constant, never model-controlled.
- The labeled fault-injection scenario (orchestrator-generated, trusted code)
  is exempt from the allowlist but not from format/package checks, so the
  still-affected 3.1.2 is genuinely installed in the disposable venv and QA
  rejects it on real evidence (see live results).

### Requirement 2 — structured QA evidence
- `verify_patch()` returns: `install_ok`, `version_inspection_ok`,
  `installed_version`, `pytest_returncode`, `junit` (parsed from pytest's
  `--junitxml` report: per-test statuses, failures/errors/skipped counts),
  `required_tests_ok`, `artifact_sha256`, `installer_invoked`.
- `qa_agent._deterministic_checks()` evaluates 9 boolean checks from that
  structure — no output-text heuristics. `installed_version` is never fallen
  back to the proposed version: inspection failure fails the run.
- Required tests: all four (`test_render_greeting_escapes_html`,
  `test_xmlattr_renders_normal_attributes`,
  `test_xmlattr_rejects_space_in_key`, `test_xmlattr_rejects_slash_in_key`)
  must be present with status "passed"; any missing/skipped/errored test fails.
- Model approval must be a strict JSON boolean — string `"false"` (or any
  other truthy non-boolean) yields `approval: False` with a "Malformed model
  output" reason. Deterministic veto overrides any model approval.

### Requirement 3 — honest workflow states and timing
- Statuses: `blocked` (Sentry non-actionable — no patch attempts created),
  `remediation_failed` (all attempts rejected — no `verified_patch`, no
  `elapsed_alert_to_tested_patch_ms`, approval impossible),
  `tested_patch_awaiting_human_approval` (only when a QA-approved attempt
  exists), then `human_approved`, `simulated_deployed`.
- `elapsed_alert_to_tested_patch_ms` is set only on genuine QA success; it is
  `None` for blocked/failed runs (mock-tested).

### Requirement 4 — real patch artifact
- `build_artifact(version)` produces `original_requirements`
  (`jinja2==3.1.2`), `proposed_requirements`, a readable unified `diff`, and
  `sha256` hashing all three contents.
- QA installs the exact proposed file; verification binds `artifact_sha256`.
- `approve()` binds approval to the hash and cross-checks proposal ==
  installed == incident `verified_patch` versions and hash; any mismatch
  rejects approval.
- `simulate_deploy()` rechecks the approved hash AND the incident's verified
  patch hash/version before recording the simulated release; post-approval
  mutation of either invalidates the release (mock-tested).

### Requirement 5 — IDs, POST, concurrency, export
- Incident IDs are `uuid.uuid4()` strings (mock-tested for UUID parseability).
- `/workflow`, `/incident/{id}/approve`, `/incident/{id}/simulate-deploy` are
  POST; GET state-changers were removed.
- Per-incident `threading.Lock` protects approve/simulate transitions; a
  store lock guards the incident map.
- The global `/export` (latest-incident) route was removed; export is by
  explicit incident ID at `/export/{incident_id}` only.
- These changes do NOT make the application ready for public hosting.

### Requirement 6 — mock-based automated tests
`tests/test_mock_acceptance.py`: 27 tests, injected mocks for
engineer/sentry/runner/QA-model — zero model calls, zero package installs.
Coverage: boundary rejection (wrong package, non-allowlisted version,
malformed version), valid proposal reaches installer, fault injection
installs and is rejected, missing installed metadata, nonzero pytest rc,
test errors, skipped tests, missing required test, version pin mismatch,
version below policy, string "false", malformed model output, model approval
cannot override deterministic failure, good evidence passes, both-attempts-
rejected leaves no success/timing, benign and uncertain incidents create no
patch attempts, verified patch sets status+timing, UUID IDs, approval
isolation, repeated approval rejected, artifact and version mutation
invalidate release, deterministic-failure approval veto, remediation_failed
approval veto, `build_artifact` bad-version rejection.

### Verification results (actual)

Mock suite (no model, no installs):
```
$ python tests/test_mock_acceptance.py
Ran 27 tests in 0.004s
OK
```

Live bounded check (real Ollama qwen3:4b + real disposable-venv installs),
run once after the mock suite passed:
```
$ python tests/live_workflow_check.py
Workflow wall time: 25.3s
Incident ID: 9efefb4b-e217-44c1-a5b6-c908d7f3ae2f
Status: tested_patch_awaiting_human_approval
Alert-to-tested-patch ms: 25324.87

Attempt 1 (fault_injection=True):
  proposal: jinja2==3.1.2 (FAULT INJECTION)
  installer invoked: True
  installed: 3.1.2
  pytest rc: 1
  junit: 2 passed, 2 failed (test_xmlattr_rejects_space_in_key,
        test_xmlattr_rejects_slash_in_key failed)
  artifact sha256: 97669cb1754359b2…
  QA approval: False
  QA reason: Deterministic evidence checks failed: version_satisfies_policy,
        pytest_returncode_zero, required_tests_all_passed, no_failures

Attempt 2 (fault_injection=False, model call):
  proposal: jinja2==3.1.4 (model reason: addresses both CVEs)
  installer invoked: True
  installed: 3.1.4
  pytest rc: 0
  junit: 4 passed, 0 failed/errored/skipped
  artifact sha256: 27cc3bd765dd79dd…
  QA approval: True

Approval: True
Simulation: True
Export disclaimer present: True
[LIVE PASS]
```

### Honest notes
- The first live run after the rewrite boundary-blocked the fault-injected
  3.1.2 at the allowlist; this weakened the required bad-patch-rejection
  scenario. Fixed by exempting the trusted orchestrator-generated fault
  injection from the allowlist (not from package/format checks), then
  re-running the mock suite (27/27) and one live run (PASS).
- Two mock tests initially failed due to test-fixture bugs (missing
  `fault_inject_first=False` and an unset `inspection_ok` flag); fixed in the
  tests, not by weakening the code.
- Old checkpoint test suites were deleted because they contained placeholder
  checks printing PASS without exercising behavior.
- Known limitation: the app stores incidents in memory only and has no auth;
  it is NOT ready for public hosting.

## Checkpoint 1

### Files created
- `requirements.txt`
- `app/main.py`
- `tests/test_checkpoint1.py`
- `MEL_WORK_LOG.md`

### Verification
- `/health` returns `{"status":"ok","service":"night-shift","checkpoint":3}` (updated by checkpoint 3)
- `/sentry-classify` returns valid classification from Ollama `qwen3:4b`
- Sequential call timings: ~1712 ms and ~1536 ms

## Checkpoint 2

### Files created / modified
- `app/ollama_client.py` — shared Ollama client with `think: false` and JSON format
- `app/sentry_agent.py` — three synthetic fixtures (actionable, benign, ambiguous)
- `app/engineer_agent.py` — constrained Jinja2 upgrade proposals with allowlist
- `app/qa_agent.py` — independent verification with deterministic veto
- `app/isolated_runner.py` — disposable venv verification runner
- `app/workflow.py` — end-to-end orchestrator with fault-injection recovery
- `app/main.py` — FastAPI endpoints
- `fixtures/test_app/` — isolated test fixture with regression tests
- `tests/test_checkpoint2.py` — acceptance tests
- `MEL_WORK_LOG.md`

### Advisory verification
- **GHSA-h5c8-rqwp-cp95** (CVE-2024-22195): affected `< 3.1.3`, patched `3.1.3`
- **GHSA-h75v-3vvj-5mfj** (CVE-2024-34064): affected `< 3.1.4`, patched `3.1.4`
- PyPI release dates confirmed.
- Remediation target: **jinja2==3.1.4** (historical target for the named advisories).

## Checkpoint 3

### Files created / modified
- `app/approval.py` — human approval and simulated deployment gates
- `app/main.py` — new `/incident/{id}/approve`, `/incident/{id}/simulate-deploy`, `/incident/{id}`, `/export/{id}` endpoints; new dashboard UI
- `tests/test_checkpoint3.py` — checkpoint 3 acceptance tests
- `MEL_WORK_LOG.md` — this update

### Implementation
1. **Human approval gate**
   - Approval is stored on the incident object and bound to a specific incident ID and patch.
   - Allowed only when `status == tested_patch_awaiting_human_approval`, a QA-approved attempt exists, and deterministic checks (`version_ok`, `tests_passed`) are True.
   - Rejects approval for failed, uncertain, missing, or already-approved incidents.
   - Records actor, UTC timestamp, incident ID, and patch version.
   - Approval is non-transferable; second approval on the same incident returns an explicit error.

2. **Simulated deployment**
   - Separate `/incident/{id}/simulate-deploy` endpoint gated on the incident's own human approval record.
   - Labeled as simulation everywhere (endpoint name, response disclaimer, UI button).
   - No external commands or infrastructure contacted.
   - Records the simulated action in the incident timeline.

3. **Dashboard**
   - Visual timeline: Sentry → Engineer → QA → Human → Simulated deployment.
   - Color-coded step status.
   - Shows fault injection badge, installed version, QA approval/rejection, elapsed alert-to-tested-patch time.
   - Expandable sections for full test output and raw model responses.
   - Progress text while workflow runs; buttons disabled during execution.
   - Benign, actionable, ambiguous triage controls.
   - Synthetic-input and historical-remediation labels preserved.

4. **Evidence export**
   - `/export/{incident_id}` returns JSON with evidence disclaimer.
   - Contains full incident timeline, full test output, raw model responses, approval, and simulation records.
   - Engineer's prompt test_output excerpt is truncated to 500 characters; full output remains in the verification object.
   - No credentials included.

### Commands run
- Wrote `app/approval.py`, updated `app/main.py`, wrote `tests/test_checkpoint3.py`.
- Restarted uvicorn on 127.0.0.1:8000.
- `python tests/test_checkpoint3.py`
- `python tests/test_checkpoint2.py`
- `python tests/test_checkpoint1.py`
- Manual end-to-end verification of workflow, approval, simulation, and export.

### Verification results

#### Checkpoint 3 tests
```
[PASS] /health
[PASS] simulation blocked before approval
[PASS] uncertain incident does not auto-remediate
[PASS] approvals are incident-bound and non-transferable
[PASS] successful approval and simulation
[PASS] exported records include approval, simulation, test output, raw model responses
[PASS] approval reuse rejected
All checkpoint 3 checks passed.
```

#### Checkpoint 2 tests (preserved)
```
[PASS] /health
[PASS] benign filtering
[PASS] uncertainty blocks remediation
[PASS] bad-patch rejection and corrected success (final version 3.1.4)
[PASS] elapsed time recorded (26100.96 ms)
[PASS] failed tests override model approval
[PASS] malformed-output safeguard (schema enforced)
All checkpoint 2 checks passed.
```

#### Checkpoint 1 test (preserved)
```
[PASS] /health
[PASS] /sentry-classify — classification=INFO in 2339.73 ms
All checks passed.
```

#### Manual end-to-end verification
- Workflow status: `tested_patch_awaiting_human_approval`
- Fault injection first attempt: True
- Final patch: `jinja2==3.1.4`
- Approval: True
- Simulation: True
- Export includes disclaimer, human_approval, simulated_deployment, and full test output.

## Hosting recommendation for a genuinely executable public demo

A public demo needs both real model inference and disposable verification, which the current local-only Ollama setup cannot provide directly. Recommended architecture:

1. **Model inference**
   - Use a remote inference provider (e.g., OpenAI API, Anthropic API, Google Vertex, or a hosted Ollama instance) with the API key stored only server-side.
   - Alternatively, self-host a small model (e.g., Llama 3.2 1B/3B or Qwen3) on the same VPS/container as the backend for deterministic availability.
   - The Sentry/Engineer/QA prompts remain identical; only the `OLLAMA_URL` and authentication change.

2. **Disposable verification**
   - Run the backend in a container or VPS that can spawn isolated processes.
   - Use temporary directories or ephemeral Docker containers for each verification run.
   - Enforce network egress restrictions so disposable environments cannot contact internal infrastructure.
   - Do not install vulnerable packages in the backend image; install them only in the disposable environment.

3. **Hosting platforms**
   - **Render / Railway / Fly.io**: easy FastAPI deployment; choose a plan with enough RAM for model inference if self-hosting.
   - **Hugging Face Spaces / Replicate**: suitable for model hosting if using a remote inference API.
   - **Self-hosted VPS (e.g., Hetzner, DigitalOcean)**: best for genuinely executable verification because you control process/container isolation.

4. **Security considerations**
   - Bind the public demo to HTTPS only.
   - Store secrets in environment variables, never in the UI or exports.
   - Keep the human approval gate server-side.
   - Do not expose the Ollama API port publicly; keep it localhost or behind an authenticated internal network.
   - Rate-limit `/workflow` to prevent abuse of model/verification resources.

## Status
- Checkpoint 3 complete.
- Service remains localhost-only (`--host 127.0.0.1`).
- No external publishing or deployment performed.

## Checkpoint 4 (correction pass)

### Corrections
1. **Progress refresh**: after approval and simulated deployment, the dashboard
   re-fetches the incident and renders progress from the current status via
   `statusLabel()` — no longer stuck on "awaiting human approval".
2. **Timeline stage states**: each stage now shows explicit text state
   ("Recovered after rejected attempt", "Awaiting approval", "Ready to
   simulate", "Simulated (no real deployment)", etc.). Pending deployment now
   looks pending, not failed.
3. **Human-readable duration**: dashboard shows alert-to-tested-patch as
   "28.4 seconds"; the export preserves exact milliseconds
   (e.g. `28345.69`).
4. **Advisory severity from metadata**: Sentry now emits `advisory_severity`
   ("MODERATE", from the GitHub Security Advisory ratings of
   GHSA-h5c8-rqwp-cp95 and GHSA-h75v-3vvj-5mfj) as the validated severity.
   The model's own severity claim is kept only as
   `model_assessed_severity` / raw evidence, never as authoritative.
   Model-assessed `operational_priority` remains separate.
5. **Approval isolation test**: approve A, leave QA-passed B unapproved, deploy
   B → rejected with "requires explicit human approval". Repeated deployment
   of A is a separate test and is rejected with "already recorded".
6. **Patch-change invalidation**: `approve()` now rejects when the proposal
   version differs from the QA-verified installed version ("Approved patch
   mismatch"); `simulate_deploy()` rejects when the approved patch no longer
   matches the current verified patch ("no longer matches"). No public API
   can mutate a patch (by design), so these guards are tested directly
   against the approval module with tampered incidents.

### Files modified
- `app/main.py` — statusLabel/formatMs helpers, explicit timeline states,
  progress refresh after approval/deployment, seconds display.
- `app/sentry_agent.py` — `advisory_severity` from trusted metadata
  (ADVISORY_SEVERITY = "MODERATE"); `model_assessed_severity` kept separate;
  raw model output unchanged.
- `app/approval.py` — proposal/installed match check on approve; approved
  patch vs current verified patch check on simulate_deploy.
- `tests/test_checkpoint4.py` — new acceptance tests.

### Verification results
```
[PASS] approval isolation: unapproved incident B cannot deploy
[PASS] repeated deployment of A rejected
[PASS] patch change after approval invalidates deployment
[PASS] approval rejected when proposal and installed version diverge
[PASS] advisory severity from metadata (MODERATE); model claim preserved separately in raw
[PASS] export preserves exact milliseconds (28345.69)
All checkpoint 4 checks passed.
```

Preserved regression suites:
```
[PASS] All checkpoint 3 checks passed (7/7)
```

### Status after checkpoint 4
- Checkpoint 4 complete.
- Service remains localhost-only (`--host 127.0.0.1`).
- No external publishing or deployment performed.

## Checkpoint 5 — grounded Engineer investigation protocol (Mel-authored)

Recovered the interrupted implementation in `<repo-root>/night-shift-mel`
without reading or copying source from `<repo-root>/night-shift`.
All work remains in this Mel session's directory and preserves the existing
Git commits/provenance.

### Two-phase investigation protocol (`app/engineer_agent.py`)
1. **Model calls for tool operations**: the Engineer model is prompted to
   return a single JSON object with `tool_calls` (`list_files`, `read_file`).
2. **Python validates and executes restricted tools**: `_call_tool` dispatches
   only the two read-only tools in `app/eng_tools.py`, which resolve direct
   children of `fixtures/test_app` and reject traversal, parents, subdirectories,
   and hidden files.
3. **Second model call receives actual results**: not used in the current
   single-turn design — instead, the first model response includes findings,
   citations, and a proposal; Python verifies citations against the real tool
   results and recomputes the grounding gate. This satisfies the bounded task:
   model-generated claims of tool execution are not accepted as evidence.
4. **Mandatory grounding gate** (`_grounding_problems`):
   - at least one `.py` source file must be read
   - a `requirements*` file must be read
   - `declared_version` must equal the fixture pin (`3.1.2`)
   - evidence must be non-empty
   - every piece of evidence must be verified against a `read_file` result
   - at least one verified citation from application source
   - at least one verified citation from the dependency declaration
5. **Bounded retry**: one retry on grounding failure; after that,
   `proposal=None`, `needs_human_review=True`, and the workflow stops before
   any installer/approval/release step.
6. **Function contracts preserved**: `propose_upgrade(advisory_context,
   previous_rejection, model_fn=None)` still returns the same top-level dict
   shape with added `investigation` and `raw` fields.

### New file `app/eng_tools.py`
Fixed read-only tool implementations:
- `list_files()` — direct files only; rejects `directory` argument
- `read_file(path='...')` — bare file names only; rejects path separators,
  parent references, hidden files, absolute paths, and files outside the
  fixture directory.

### New file `tests/test_grounded_live.py`
Focused mock tests with no real model calls:
- eng_tools boundary tests (6 tests)
- grounding gate tests (11 tests) covering pass, missing source,
  listing-only, empty evidence, unread citations, wrong declared version,
  workflow blocking, bounded retry pass, model error handoff, and tool-call
  recording
- live-progress endpoint tests (4 tests) for `/workflow` background + polling

### Existing files modified
- `app/engineer_agent.py` — replaced simple proposal with grounded
  investigation + bounded retry + needs_human_review handoff.
- `app/workflow.py` — stops the remediation loop on
  `needs_human_review`, keeping `verification=None` and `qa=None` so no
  installer is invoked.
- `app/main.py` — dashboard renders `needs_human_review` status, shows
  investigation details (files read, declared version, grounding/citation
  problems, tool calls, evidence), and keeps staging controls disabled when
  no verified patch exists.

### Verification results
```
$ source .venv/bin/activate && python -m pytest tests/test_grounded_live.py -v
21 passed in 65.78s

$ source .venv/bin/activate && python -m pytest tests/test_mock_acceptance.py tests/test_staging.py -v
80 passed in 0.66s
```

### Status after checkpoint 5
- Grounded investigation protocol complete and verified with mocks.
- No live model workflows run during this task.
- No dashboard redesign or alternative architectures explored.
- Service remains localhost-only.
- Paused after logging; awaiting next instruction.

## Checkpoint 6 — slow-test diagnosis, two-phase protocol repair, bounded live run

### 1. Why `test_grounded_live.py` took 65.78s
`--durations` on the suite showed the grounded-gate tests are effectively
instant (<5 ms each); the time was spent in two `TestLiveProgress` tests
that were not actually mock-only:
- `test_workflow_starts_background_and_polls` — `POST /workflow` spawned a
  real background thread calling `run_incident`, which calls the real
  qwen3:4b model (Sentry ~2.7 s, Engineer multi-second, QA) and does real
  disposable-venv installs. The test polled up to 60×1 s (31.28 s).
- `test_workflow_sync_endpoint_exists` — the old assertion only checked
  `!= 404`, so the endpoint really executed the full live workflow (~25.32 s).

### 2. Fix (no weakened assertions)
Both tests now `patch("app.main.run_incident")` with a fixture incident.
Polling, duplicate-guard, status-not-found, and endpoint-status assertions
are still exercised for real against the TestClient. Suite time dropped:
- Before: 21 passed in 65.78s (then 63.24s on re-measure).
- After: 21 passed in ~5–6s, with only `test_grounding_failure_blocks_installation`
  (~5s) remaining — that one is a real workflow over mocks (fault-injection
  attempt does a genuine `verify_patch` install/pytest; it is not a model call).

### 3. Bounded live run (ONE real recovery)
Ran `tests/live_staging_check.py` against the local Ollama qwen3:4b. The run
failed at the grounding gate exactly as the task anticipated; the evidence
was preserved and no corrected-patch installation, approval, or staging step
was reached:
- Incident `ee7c5f18-b469-4028-9d70-bbd02a4b522e`, wall time 24.1 s.
- Attempt 1 (fault injection): `jinja2==3.1.2`, pytest rc=1, 2 failures,
  QA rejected — bad-patch rejection confirmed.
- Attempt 2 (model): `needs_human_review`; `verification=None`, `qa=None`
  (no installer invoked). Grounding problems:
  `['model error in tool loop: Model repeated an already-executed tool request; stopping the tool loop to avoid an infinite repeat.']`
- `tests/diagnose_phases.py` showed why: in Phase 1 the model reliably
  returns valid tool-call JSON but re-issues `list_files` every turn and
  never requests `read_file` for the application source or the dependency
  declaration, so the bounded loop hits its repeat guard and grounding
  fails honestly instead of burning turns.

### 4. Two-phase protocol repair (still in Mel, no source copied)
`app/engineer_agent.py` was rewritten to the two-phase protocol:
- Phase 1 — tool operations only: the model is asked for `tool_calls` per
  turn; Python validates and executes only the restricted read-only tools
  (`app/eng_tools.py`). A bounded turn loop runs up to `MAX_TOOL_TURNS`;
  an anti-repeat guard stops the loop if the same tool+args request is seen
  twice; after the first successful read the prompt asks the model to end
  the investigation if it has enough evidence.
- Phase 2 — findings + proposal: a separate model call receives the actual
  tool results (file lists, file contents, errors) and returns
  `declared_version`, `evidence` citations, `proposal`, and `rationale`.
  Citations are verified against the real tool results, not model claims.
- The grounding gate and one-bounded-retry `needs_human_review` handoff are
  unchanged; `propose_upgrade` keeps its function contract.

### Verification results
```
$ python -m pytest tests/test_grounded_live.py tests/test_mock_acceptance.py tests/test_staging.py -q
101 passed in ~6s
$ python tests/diagnose_phases.py   # real qwen3:4b
Phase 1 returns valid tool-call JSON; model repeats list_files and never
requests the reads; anti-loop guard stops the loop; grounding fails honestly.
$ python tests/live_staging_check.py  # ONE real recovery
Attempt 1 (fault injection) QA-rejected; Attempt 2 needs_human_review with
verification=None/qa=None; export and timing preserved. No corrected-patch
installation reached.
```

### Files changed this checkpoint
- `tests/test_grounded_live.py` — mock `run_incident` in live-progress tests;
  scripted two-phase mocks for the grounding suite; suite now ~6s.
- `app/engineer_agent.py` — two-phase tool-loop + findings protocol, bounded
  turn loop with anti-repeat guard.
- `tests/diagnose_phases.py` — new live diagnostic script (real model,
  prints per-turn raw responses).

### Status after checkpoint 6
- Slow tests fixed; mock suite verified fast and green (101 passed).
- Two-phase protocol implemented and mock-verified.
- Live run performed once; grounding failure preserved with exact reason
  (model repeats `list_files`, never reads source/requirements). This is a
  model-behavior limit of the local qwen3:4b under the current prompts, not
  a protocol bug: Python correctly refuses fabricated evidence and hands off
  with no installation.
- No model downloads, dashboard redesign, outside source copying, publishing,
  or external deployment.
- Paused after logging.

## Checkpoint 8 — acceptance reconciliation and dashboard operations-console polish

### Acceptance-suite reconciliation
The apparent acceptance count regression was not four lost tests:
- Current `pytest --collect-only tests/test_mock_acceptance.py` collects **64**
  tests; `pytest tests/test_mock_acceptance.py -q` confirms **64 passed**.
- The baseline commit `8c5dc83` contained two classes named
  `TestWorkflowScenarios` (at historical lines 525 and 657). The second class
  duplicated five tests already defined by the first:
  `test_full_workflow_verified`, `test_fault_injection_reaches_installer_and_rejected`,
  `test_both_attempts_rejected`, `test_blocked_incident_no_attempts`, and
  `test_engineer_rejection_prompt_uses_current_fields`.
- The four tests initially thought lost —
  `test_qa_true_with_invalid_evidence_leaves_failed`, `test_uuid_incident_id`,
  `test_needs_human_review_blocked`, and `test_model_false_string_rejected` —
  were never in that duplicate class and remain collected in the current
  `TestWorkflowScenarios`.
- A direct comparison of the repair commit's test method list with the
  current list is empty. No distinct assertion coverage was removed, renamed,
  or needs restoration. The earlier “60” was an incorrect reporting figure,
  not a collected-test result.

Actual collection/execution:
```
$ python -m pytest tests/test_mock_acceptance.py --collect-only -q
64 tests collected
$ python -m pytest tests/test_mock_acceptance.py -q
64 passed
$ python -m pytest tests/test_staging.py -q
16 passed
$ python -m pytest tests/test_grounded_live.py -q
26 passed
```

### Dashboard polish (`app/main.py`, frontend only)
Reworked the existing embedded dashboard without changing endpoints,
workflow logic, approvals, manifests, or staging safeguards:
- Dark-navy local operations console with readable system typography and
  restrained teal state/action accents.
- Clear primary “Run local recovery” action; live polling status, incident
  id, agent/event counts, measured alert-to-tested-patch timing, and an
  accurate six-stage recovery path.
- Patch review before approval shows verified package/version, installed
  version, artifact SHA-256 prefix, requirements diff, verified citations,
  and structured per-test results.
- QA decision, explicit human approval, real local staging, and optional
  simulation are distinct states/actions. Local staging is primary after
  approval; simulation is secondary and explicitly records no deployment.
- Evidence reads are labeled orchestrator-selected; Engineer is described
  as interpreting the recorded numbered contents. The detail panel keeps
  actual executed reads, citations, JUnit, deterministic checks, model output,
  and logs expandable. Raw model/log content is escaped into `<pre>` text;
  evidence remains downloadable through the existing export endpoint.
- Persistent disclosures cover synthetic input, deliberate fault injection,
  historical remediation, localhost-only staging, and intentionally
  unauthenticated local-demo approval.

### Verification
```
$ python -m py_compile app/main.py
$ python -m pytest tests/test_grounded_live.py tests/test_mock_acceptance.py tests/test_staging.py -q
106 passed in 6.08s
```
Rendered browser verification on `http://127.0.0.1:8000/` confirmed the
console shows recovery action, disclosures, live-progress region, patch
review controls, local-staging-primary language, evidence/logs disclosure,
and the complete synthetic/local-only/unauthenticated-approval notices.
No new live package-installing recovery was run: dashboard changes are
frontend-only and existing live staging verification remains recorded in
checkpoint 7.

### Status after checkpoint 8
- Acceptance count reconciled: 64 actual acceptance tests; no distinct
  coverage lost.
- Dashboard polished without backend safeguard changes.
- Mock suites pass (106). Local server stopped after browser verification.
- No public hosting, external deployment, model change, or source copying.
- Paused after commit.

## Checkpoint 7 — deterministic orchestrator-selected evidence collection

Replaced the model-driven tool loop (which the local qwen3:4b could not
drive reliably — it repeated `list_files` and never requested the reads)
with a transparent, deterministic evidence-collection step, without
weakening any evidence gate.

### Protocol (`app/engineer_agent.py`)
- **Orchestrator-selected evidence collection**: `_collect_evidence()` reads
  the two known fixture targets through the existing restricted read-only
  tools (`app/eng_tools.read_file`): `requirements-affected.txt` (affected
  dependency declaration) and `main.py` (relevant application source). The
  selection is fixed file names only — findings, citations, and the
  remediation version are never hardcoded. Each executed read records the
  tool call, its actual result, and the numbered contents. Every read is
  labeled `selected_by: "orchestrator (deterministic evidence collection,
  not model-selected tool calls)"` in the investigation record.
- **Model interpretation**: a single model call receives the recorded reads
  with numbered contents and returns `declared_version`, `applicability`,
  `uncertainty`, evidence citations, a proposal, and a rationale. Citations
  are validated against the recorded numbered contents — fabricated or
  out-of-range citations are rejected.
- **The repeated-listing tool loop is removed** along with the anti-repeat
  guard; `MAX_TOOL_TURNS` is gone. No new tools, no model change.
- **Unchanged safeguards**: mandatory grounding gate (source + declaration
  read, declared version must match the pin, non-empty verified evidence
  from both files), citation validation, one bounded retry, honest
  `needs_human_review` handoff with `proposal=None`, boundary validation,
  declared-vs-installed distinction preserved (declared 3.1.2 in the
  declaration; installed version only ever comes from the isolated
  runner's inspection).

### Mock tests (`tests/test_grounded_live.py`, rewritten)
New/updated coverage of exactly the requested cases, all with mocked
models and real fixture reads:
- evidence-read failure (missing file) recorded as a collection problem →
  grounding fails → handoff with no installer call
- fabricated citation (file never collected) rejected
- out-of-range line citation rejected
- missing application-source evidence fails the gate
- empty evidence, wrong declared version, malformed model output (response
  not a JSON object) → handoff
- evidence-read failure blocks installation in the workflow
- successful grounded interpretation passes, records numbered contents,
  and prompts the model with the real numbered lines
- declared vs installed/patch version distinction asserted
- bounded retry-then-pass; model error handoff; collected reads recorded
- eng_tools boundary tests unchanged; live-progress endpoint tests unchanged

Preserved acceptance suites: `tests/test_mock_acceptance.py` (60) and
`tests/test_staging.py` (16) untouched and passing.

### Verification results
```
$ python -m pytest tests/test_grounded_live.py tests/test_mock_acceptance.py tests/test_staging.py -q
106 passed in 6.20s

$ python tests/live_staging_check.py   # ONE bounded real recovery
Incident dea2bd9e-89b6-4055-aa96-b6205df515d3, wall 42.2s
  Attempt 1 (fault injection): jinja2==3.1.2, rc=1, failures=2, QA=False (bad patch rejected)
  Attempt 2 (model): jinja2==3.1.4, rc=0, failures=0, QA=True
    files_read: ['requirements-affected.txt', 'main.py']
    declared: 3.1.2 / 3.1.2 — grounding_problems: None, citation_problems: []
Approval: True. Staging release: True (port 51598), health jinja2==3.1.4
MarkupSafe==2.1.5, smoke all-true; duplicate release rejected; stop confirmed;
export preserves staging events [release_succeeded, release_failed, stop].
[LIVE STAGING PASS]
```

Model interpretation from the live run (not hardcoded — model output):
- applicability: applies — declared 3.1.2 is below both patch levels.
- uncertainty: exact failure conditions not fully detailed in the evidence.
- evidence: requirements-affected.txt:4 (pin), main.py:10 (xmlattr on user keys).
Export preserved: `elapsed_alert_to_tested_patch_ms` 40345.29, verified_patch
jinja2==3.1.4 artifact sha256 e01113fd18eb4942…, requirements diff
`-jinja2==3.1.2 / +jinja2==3.1.4`.

### Files changed this checkpoint
- `app/engineer_agent.py` — deterministic evidence collection + single
  interpretation call; tool loop removed.
- `tests/test_grounded_live.py` — rewritten for the new protocol with the
  requested failure cases.
- `tests/diagnose_phases.py` — deleted (superseded by the deterministic
  collection; no longer needed).
- `MEL_WORK_LOG.md` — this entry.

### Status after checkpoint 7
- Grounded investigation reliable: the evidence reads are deterministic and
  recorded with numbered contents; the model only interprets.
- One bounded real recovery completed end-to-end: bad-patch rejection →
  grounded corrected patch → tests → approval → local staging
  health/smoke → stop → export preserved.
- No models changed, no tools added, no redesign, no publishing, no
  external deployment. Paused after commit.

## Checkpoint 9 — submission materials (no publishing)

### Files created
- `README.md` — customer problem, agent responsibilities and trust model,
  orchestrator-selected evidence collection (including the honest history
  of the model-driven tool-loop failure), setup/run instructions, local
  staging flow, test commands, limitations, advisory sources, and a
  provenance section pointing at PROVENANCE.md.
- `docs/DEMO_OUTLINE.md` — three-minute demo using the actual measured
  results of the checkpoint-7 live run (42.2 s wall, 40,345.29 ms
  alert-to-tested-patch, fault-injected attempt 1 rc=1/2 failures,
  attempt 2 jinja2 3.1.4 4/4, staging health jinja2 3.1.4/MarkupSafe
  2.1.5, smoke all-true), with explicit on-screen labeling of fault
  injection and historical remediation and instructions to show any
  grounding handoff honestly rather than hide it.
- `docs/SUBMISSION_CHECKLIST.md` — public repo, demo video, Mel-session
  video/export, work log, test proof, live-run proof, customer-quote
  permission (none used today), Sunday curveball (rehearse the three
  deliberate fail-closed paths), and pre-publish final pass. Publishing
  items are deliberately unchecked.
- `docs/example-export.json` — sanitized example of the successful run's
  export: real structure and semantics, real fixture file contents and
  numbered reads, with example-marked incident id, timestamps, and hashes;
  sanitization note included.
- `docs/SUBMISSION_AUDIT.md` — tracked-file audit (below).

### Tracked-file audit findings (no sensitive values exposed)
- Secrets: none in any tracked file (no keys/tokens/passwords/PEM).
- Private session records: none tracked; `*.events.jsonl`,
  `mel-conversation*.json*`, `events/` are gitignored.
- Environments/caches: none tracked (`.venv/`, `__pycache__/`,
  `.pytest_cache/` ignored).
- Runtime artifacts: none tracked; `artifacts/` (41 incident dirs) and
  `staging/` are gitignored and stay local.
- Logs: none tracked (`*.log` ignored).
- Personal paths: only `MEL_WORK_LOG.md` contains `<home>/…`
  absolute paths — part of the truthful provenance record; publish as-is
  or rewrite to `~/` in a documented commit (decision left open).
- Stray temp files: none tracked.

### Provenance and claims
- PROVENANCE.md states only the permission actually received (returning to
  the pre-outside-work boundary and finishing in Mel); no organizer
  acceptance of the submission is claimed anywhere.
- Git history records Mel-authored commits; the work log records tool
  contributions (Mel terminal agent only in this tree).

### Verification results (this checkpoint)
```
$ python -c "import json; json.load(open('docs/example-export.json'))"
example-export.json: valid JSON
$ python -c "from app.main import app"
app imports OK
$ python -m pytest tests/test_grounded_live.py tests/test_mock_acceptance.py tests/test_staging.py -q
106 passed in 6.11s   (26 grounded + 64 acceptance + 16 staging)
```
No live package-installing run was performed: this checkpoint adds
documentation only, and the recorded live evidence from checkpoint 7
remains the proof.

### Status after checkpoint 9
- Submission materials prepared; nothing published or deployed.
- Publishing decisions (repo visibility, license, work-log path wording,
  customer-quote permission) remain open on the checklist.
- Paused after commit.

## Checkpoint 10 — publication preparation (no publishing)

### Changes (documentation only; no application behavior touched)
1. **Personal paths redacted from tracked docs.** `MEL_WORK_LOG.md` and
   `docs/SUBMISSION_AUDIT.md` previously contained `<home>/<username>/…`
   absolute paths; these are now `<repo-root>/night-shift-mel`,
   `<repo-root>/night-shift`, and `<home>/…` placeholders. A grep of all
   tracked files for the username returns nothing. Factual attribution
   and recovery history are otherwise unchanged. The original detailed
   work log is preserved privately outside the repository
   (`<repo-root>/night-shift-mel-private/MEL_WORK_LOG_FULL.md`).
2. **`docs/example-export.json` relabeled.** Its `_sanitization_note` now
   opens "ILLUSTRATIVE SANITIZED SAMPLE — NOT RAW TEST PROOF" and states
   that identifiers, timestamps, and hashes are placeholders that cannot
   be independently verified; it points readers to the private genuine
   capture and to rerunning the workflow for verifiable proof.
3. **Genuine export preserved privately; sanitized proof copy prepared.**
   The application serves exports from an in-memory store, so the full
   byte-for-byte export of the successful run was not on disk; what is
   preserved is the exact run-time readback from
   `/export/dea2bd9e-89b6-4055-aa96-b6205df515d3` (incident id,
   40345.29 ms timing, fault-injected attempt rc=1/2 failures, model
   attempt jinja2 3.1.4 4/4, applicability/uncertainty, both citations,
   artifact sha256 prefix `e01113fd18eb4942`, requirements diff, staging
   port/health/smoke, 42.2 s wall). The private folder holds:
   `MEL_WORK_LOG_FULL.md`, `genuine-export-capture.json`,
   `sanitized-proof-copy.json` (redacting only the incident id and
   loopback port; no replacement evidence invented), and a README
   describing all three. The on-disk artifact directory whose manifest
   digest begins `e01113fd18eb4942` remains under the gitignored
   `artifacts/` tree as corroborating physical evidence.
4. **Submission checklist corrected.** The "Sunday curveball" is now
   correctly described as the organizer's actual new request, left
   pending until received and implemented — not our own failure-path
   rehearsal (rehearsal remains listed under Demo video preparation).
5. **`docs/SUBMISSION_AUDIT.md` updated** for items 1–3 (finding 6:
   personal paths none remaining; example-export paragraph: illustrative
   sample + private genuine/proof copies; conclusion updated).

### Verification results
```
$ python -c "import json; json.load(open('docs/example-export.json'))"
example-export.json: valid JSON
$ python -c "import json; …private files…"
private JSONs: valid
$ git ls-files | xargs grep -l '<username>'
(no matches — no personal paths or usernames in tracked files)
$ python -m pytest tests/test_grounded_live.py tests/test_mock_acceptance.py tests/test_staging.py --collect-only -q
106 tests collected   (26 grounded + 64 acceptance + 16 staging — matches README)
```
No code changed this checkpoint; the app-import and suite checks from
checkpoint 9 remain valid. No test-count churn introduced anywhere.

### Status after checkpoint 10
- Repository is publication-ready; nothing published or deployed.
- Remaining submission items (from docs/SUBMISSION_CHECKLIST.md): choose
  repo visibility/name and license, record the demo video, prepare the
  Mel-session video/export, capture a fresh timestamped full-suite run as
  test proof, await and implement the organizer's Sunday curveball, obtain
  customer-quote permission if any quote is ever used, and run the final
  pre-publish audit pass.
- Paused after commit.
