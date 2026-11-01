# Night Shift — autonomous security-remediation recovery (local-only demonstration)

Night Shift is a localhost-only operations console that demonstrates a
bounded, evidence-gated agent workflow for dependency-vulnerability
remediation: a synthetic alert is triaged, evidence is collected and
interpreted, a patch is proposed and deterministically verified in a
disposable environment, a human explicitly approves, and only then is the
approved artifact released to a **real local staging process** on
`127.0.0.1`. No public hosting, external deployment, or production release
exists anywhere in this project.

## The customer problem

A dependency advisory lands overnight. The on-call question is narrow but
consequential: *does this advisory actually apply to our application, what
version should we move to, and can we prove the patched build really works
before a human signs off?*

Unassisted LLM agents answer that question badly in a specific, repeatable
way: they **claim** to have investigated. They describe files they never
opened, cite line numbers that do not exist, and assert that tests passed
that were never run. Night Shift exists to demonstrate an alternative
shape: an agent system in which no model output is trusted until it is
checked against recorded, tool-executed evidence.

## Agent responsibilities

| Agent | Responsibility | Trust model |
|---|---|---|
| **Sentry** | Classifies the synthetic alert (actionable / benign / ambiguous) and emits the advisory context. | Advisory metadata (CVE/GHSA ids, affected range, severity) comes from trusted fixture metadata, not from the model. |
| **Engineer** | Interprets collected evidence: applicability, uncertainty, verified citations, and one allowlisted remediation proposal. | **Interprets, never collects.** Citations are validated against the recorded numbered contents of files Python itself read. |
| **QA** | Independently verifies the proposed patch in a disposable virtualenv: install, version inspection, pytest with JUnit-parsed results. | Deterministic; model approval is a strict JSON boolean and is vetoed by any deterministic failure. |
| **Human** | Explicitly approves the verified patch before any release. | Unauthenticated by design in this local demo; recorded as an event. |
| **Staging / simulation** | Local staging runs the approved artifact as a real process on `127.0.0.1` with `/health` and `/smoke` checks. Simulation is a secondary, no-deployment record. | Approval and release steps re-check the artifact manifest and dependency declarations rather than trusting prior verification. |

## Orchestrator-selected evidence collection

The most important design decision, and the reason this repository exists in
its current form: **the model does not choose which files to read.** For this
single known fixture, evidence collection is a transparent, deterministic
orchestrator step:

1. Python reads the affected dependency declaration
   (`requirements-affected.txt`) and the relevant application source
   (`main.py`) through the restricted read-only tools in `app/eng_tools.py`.
2. Each executed read is recorded — tool name, arguments, actual result, and
   **numbered contents** — and labeled
   `selected_by: "orchestrator (deterministic evidence collection, not model-selected tool calls)"`.
3. Only then is one model call made, receiving the numbered contents, which
   returns an interpretation: declared version, applicability, uncertainty,
   evidence citations, an allowlisted proposal, and a rationale.
4. Citations are validated against the recorded numbered contents. A
   citation of a file that was not read, or a line number beyond the real
   file length, is rejected. A model's *claim* of having read something is
   never evidence.
5. A mandatory grounding gate must pass: both files read, declared version
   matching the pin, non-empty verified evidence from both source and
   declaration. One bounded retry is allowed; after that the incident is
   handed to human review with **no proposal and no installation**.

An earlier iteration asked the model to drive tool calls itself. The local
model (qwen3:4b) repeatedly re-requested `list_files` and never requested
the reads, then cited files it had never opened. The system correctly
refused the fabricated citations and handed off — but the workflow never
recovered. Deterministic collection fixed the reliability problem without
weakening the gate; that history is recorded in `MEL_WORK_LOG.md`.

## Setup and run

Requirements: Python 3.9+, [Ollama](https://ollama.com) running locally on
port 11434 with the `qwen3:4b` model pulled (no downloads are performed by
this project).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` in a browser. The landing page provides product
context and links to the console at `/app`. The console offers **Analyze inbox**,
quick triage examples, live progress, the incident workspace (requirements diff,
verified citations, installed version, test results, artifact identifier),
approval and local-staging controls, and a download button for the full evidence
export.

### Local staging flow

1. **Run local recovery** — the workflow starts in the background
   (Sentry → Engineer → QA; roughly 20–45 s with a local model).
2. The first remediation attempt is **fault-injected on purpose**: the
   still-affected `jinja2==3.1.2` is installed and must be rejected on real
   JUnit evidence (2 failures) before the corrected patch is considered.
3. The Engineer's grounded proposal (`jinja2==3.1.4`) is verified in a
   disposable venv: exact pin installed, version inspected, 4/4 tests green,
   artifact manifest and SHA-256 recorded.
4. Review the patch panel, then **Record human approval**.
5. **Release to local staging** — the approved artifact runs as a real
   process on a random `127.0.0.1` port; `/health` reports the running
   versions and `/smoke` re-checks the security-relevant behaviors.
6. **Stop local staging** when done. A **Record simulation instead** button
   exists as a secondary action and records no real deployment.

### Test tooling (fresh environments)

`pytest` is deliberately NOT in the shipped application `requirements.txt`
— the runtime does not need it. Install the pinned test tooling separately:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest==8.4.2
```

## Tests

```bash
source .venv/bin/activate

# Grounded-investigation protocol, evidence gate, dashboard endpoints (mocked models)
python -m pytest tests/test_grounded_live.py -q          # 29 passed

# Sentry triage: no expected-answer leak, evidence minimums, robust JSON
python -m pytest tests/test_sentry_triage.py -q           # 13 passed

# Acceptance suite: installer boundary, artifact persistence, JUnit parsing,
# evidence gates, workflow scenarios, approval gates (all mocked)
python -m pytest tests/test_mock_acceptance.py -q        # 65 passed

# Staging lifecycle (mocked)
python -m pytest tests/test_staging.py -q                # 22 passed

# Release and verification gates (mocked)
python -m pytest tests/test_release_gates.py -q          # 20 passed

# Mixed inbox: classification, signal separation, remediation gating (mocked)
python -m pytest tests/test_inbox.py -q                  # 34 passed — matte-black refinement

# Bounded live checks (real model + real disposable installs — run manually)
# python tests/live_workflow_check.py
# python tests/live_staging_check.py
```

The mock suites make no model calls and perform no installs. The two
`live_*` scripts are manual, bounded one-shot verifications requiring a
running server and Ollama.

## Limitations

- **Synthetic inputs.** The incident, logs, and fixture application are
  synthetic; exploitability is unverified.
- **Single-package scope.** The allowlist, installer, and evidence targets
  are deliberately narrowed to `jinja2` versions relevant to the named
  advisories. This is a demonstration boundary, not a general remediation
  system.
- **Historical remediation.** `jinja2==3.1.4` is the historical fix version
  for CVE-2024-22195 / CVE-2024-34064 (2024). It is not asserted to be a
  currently secure release, and no live vulnerability data is consulted.
- **Fault injection is intentional.** Attempt 1 of every recovery
  deliberately installs the affected version to demonstrate deterministic
  rejection.
- **Localhost only.** Everything binds to `127.0.0.1`. The human-approval
  endpoint is unauthenticated by design for this local demo and must not be
  exposed.
- **Model limits.** The local `qwen3:4b` is small; the protocol is designed
  so its failures fail closed (handoff) rather than producing ungrounded
  claims.
- **In-memory store.** Incidents live in process memory; restarting the
  server clears them.

## Advisory sources

- GHSA-h5c8-rqwp-cp95 / CVE-2024-22195 — Jinja2 `xmlattr` accepts keys
  with spaces (and `/`, `>`, `=`); fixed in 3.1.3.
- GHSA-h75v-3vvj-5mfj / CVE-2024-34064 — Jinja2 `xmlattr` accepts keys
  with `/`, `>`, `=` beyond the prior fix; fixed in 3.1.4.

Both are mirrored as trusted fixture metadata in `app/sentry_agent.py`
(affected range, patch versions, severity), which is what the workflow
consumes; the model never invents advisory facts.

## Provenance and authorship

See `PROVENANCE.md` for the reconstruction boundary of this repository: it
is a Mel-only rebuild of an interrupted implementation, with the boundary,
known-baseline defects, and authorship caveats recorded. `MEL_WORK_LOG.md`
is the full working log. Git records changes, not authorship; the private
session records that support the provenance claim are deliberately not part
of this repository.
