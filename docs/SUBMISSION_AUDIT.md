# Submission audit — tracked-file review

Audit of `git ls-files` (the tree that would be published) for secrets,
private session records, environments, logs, personal paths, and runtime
artifacts. Performed 2026-11-01 on the checkpoint-8 tree; re-run before
publishing.

## Method
- Enumerated all tracked files (`git ls-files`).
- Content-scanned tracked files for secret patterns (AWS/GitHub-style key
  prefixes, `api_key`, `password`, private-key PEM headers), personal
  absolute paths (`<home>/…`), and private session-record filenames.
- Compared the on-disk tree against `.gitignore` coverage for environments
  (`.venv/`), runtime artifacts (`artifacts/`, `staging/`, `*.log`,
  `.pytest_cache/`), and raw session records (`*.events.jsonl`,
  `mel-conversation*.json*`, `events/`).

## Findings

1. **Secrets: none.** No API keys, tokens, passwords, or private keys in
   any tracked file. `app/ollama_client.py` targets
   `http://localhost:11434` with no credentials (Ollama is unauthenticated
   local-only, and the demo disclosure states this).

2. **Private session records: none tracked.** `.gitignore` excludes
   `*.events.jsonl`, `mel-conversation*.json*`, and `events/`. The raw
   session records referenced by `PROVENANCE.md` are, by design, not in
   the repository.

3. **Environments and caches: none tracked.** `.venv/`, `__pycache__/`,
   `*.pyc`, `.pytest_cache/` are ignored and absent from the index.

4. **Runtime artifacts: none tracked.** `artifacts/` (41 incident
   directories with venvs, manifests, diffs) and `staging/` exist on disk
   but are gitignored. They contain real per-run artifacts and must stay
   untracked; publishing the repo does not publish them.

5. **Logs: none tracked.** `*.log` ignored; no log files in the index.

6. **Personal paths: one file, intentional.** `MEL_WORK_LOG.md` contains
   absolute paths under `<home>/…` (the working
   directories of the Mel sessions and of the preserved original project).
   These are part of the truthful provenance record. Options before
   publishing: (a) publish as-is — the paths reveal only a username and
   project names; or (b) mechanically rewrite them to `~/night-shift-mel`
   style in a documented commit. The work log is otherwise
   documentation-only with no runtime behavior.

7. **Stray file: none.** A temporary marker file created during the
   dashboard rewrite was removed before commit and is not tracked.

## Provenance and claims
- `PROVENANCE.md` accurately describes the reconstruction boundary and
  states that private session records are not included. It does not claim
  organizer acceptance of anything beyond the permission actually
  received (returning to the pre-outside-work boundary and finishing in
  Mel).
- Git history (`8c5dc83` → `dd6f093` → `f2366be` → `50cf43b` →
  `c5b3c35` → subsequent checkpoints) records Mel-authored commits; the
  work log records tool contributions (Mel terminal agent; no other
  code-generation tools were used in this tree).
- `docs/example-export.json` is clearly labeled sanitized/example;
  placeholder ids and hashes are marked, and the file contents it embeds
  are the synthetic fixture files already public in the repo.

## Conclusion
The tracked tree contains no secrets, environments, logs, or runtime
artifacts. The only publish decision is the personal-path wording in
`MEL_WORK_LOG.md` (item 6). Nothing has been published.
