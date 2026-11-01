# Three-minute demo outline

All numbers below are the actual measured results of the recorded live run
(incident `dea2bd9e-…`, `MEL_WORK_LOG.md` checkpoint 7). Total wall time of
the recovery itself: **42.2 seconds**; alert-to-tested-patch timing
**40,345.29 ms** as exported.

## 0:00–0:20 — The problem (dashboard, idle state)
- Open `http://127.0.0.1:8000/` — dark-navy local operations console.
- One line of framing: "A vulnerability advisory lands overnight. The
  question is whether it applies, what to move to, and whether the fix can
  be *proven* before a human signs off."
- Point at the disclosures: **synthetic inputs**, **fault injection is
  intentional**, **Jinja2 3.1.4 is a historical remediation target** (2024
  CVEs), **localhost-only**, **unauthenticated approval is a local-demo
  simplification**.

## 0:20–1:00 — Run the recovery
- Click **Run local recovery**. Note the live-progress region: agents
  completing, event count, attempt count.
- While it runs, explain the trust architecture: the **model never chooses
  which files to read**. Python reads `requirements-affected.txt` and
  `main.py` itself, records the reads with numbered contents, labels them
  *orchestrator-selected*, and only then asks the model to interpret.
- When it finishes, the recovery path shows six stages. **Attempt 1 is
  fault-injected on purpose**: it installs the still-affected
  `jinja2==3.1.2` and is rejected on real evidence — **pytest rc=1, 2
  failures**. That is the system catching a bad patch, not a bug.

## 1:00–1:50 — Patch review (before approval)
- Walk the patch-review panel: proposed/verified patch `jinja2==3.1.4`,
  **installed version 3.1.4** (inspected in the disposable venv, distinct
  from the *declared* 3.1.2 in the requirements file), **artifact
  SHA-256 identifier**, the **requirements diff** (`-jinja2==3.1.2` /
  `+jinja2==3.1.4`), the **verified citations** table
  (`requirements-affected.txt:4`, `main.py:10`), and the **4/4 test
  results**.
- Emphasize: every citation was validated against the recorded numbered
  contents. A fabricated citation (file never read, or line past EOF) is
  rejected by the gate; after one bounded retry the incident hands off to
  human review with no proposal.
- Expand one evidence detail to show the raw model output rendered as plain
  text, clearly labeled as model-generated, not validated fact.

## 1:50–2:30 — Human approval and real local staging
- Click **Record human approval** — note it is a distinct, explicit gate.
- Click **Release to local staging** (primary action): the approved
  artifact runs as a **real process on 127.0.0.1**; show `/health`
  (`jinja2 3.1.4`, `MarkupSafe 2.1.5`) and `/smoke` (all three
  security-relevant checks true).
- Mention that **simulation is secondary** — a button that records no real
  deployment.
- Click **Stop local staging** to close the loop.

## 2:30–3:00 — Evidence export and limits
- Click **Download evidence JSON**: the full incident — tool-executed reads
  with numbered contents, citations, deterministic checks, JUnit results,
  artifact manifest and hashes, approval and staging events, measured
  timing (40,345.29 ms).
- Close on the honest limits: single-package scope, synthetic inputs,
  historical remediation target, localhost-only, in-memory store. "The
  claim is not that this is a production remediation system — it's that no
  model output in it is trusted until it's checked against recorded
  evidence."

## Recording notes
- Rehearse once; the recovery segment takes ~45 s of real waiting — narrate
  the architecture over it rather than cutting.
- If the local model hands off to human review instead of grounding (it can,
  by design), show the handoff honestly: that *is* the fail-closed
  behavior, then rerun. Do not edit the recording to hide a handoff.
- Label on-screen at attempt 1: "FAULT INJECTION (intentional)". Label on
  the patch panel: "historical remediation target (2024 CVEs)".
