# Recovery Provenance

## What this repository is
A reconstruction of the unfinished Mel-only state of Night Shift, created
after the organizer permitted returning to the point where outside
implementation began and finishing in Mel.

## Reconstruction boundary
- Cutoff: session event seq 1746, 2026-10-31 20:10:27 (end of Mel turn 16).
- The first outside implementation was an external agent's repair pass that
  began during the gap after that turn (20:10–20:18) and produced 64 passing
  tests. Everything from that point in the original directory is outside or
  mixed work and was NOT copied here.
- This baseline was rebuilt by replaying only verified, successful Mel
  `write_file`/`edit_file` operations recorded before the cutoff, plus two
  terminal `sed` substitutions reproduced as direct string replacements
  (`REQUIRED_TESTS[:]` → `REQUIRED_LIST[:]` at seq 1423;
  `MarkupSafe>=2.0` → `MarkupSafe==2.1.5` at seq 1654). No historical shell
  commands were executed; no files were copied from the mixed project.

## Known defects in this baseline (by design)
- `tests/test_mock_acceptance.py` is corrupted exactly as Mel left it: the
  `TestWorkflowScenarios` class is defined twice, and one assertion uses the
  typo'd status `tested_patch_awaiting_human_ability`. This is the unfinished
  state the recovery checkpoint repairs.
- The four placeholder checkpoint test suites had already been deleted by
  Mel before the cutoff and are absent.

## Authorship caveat
Git records changes, not authorship. This commit records the reconstructed
baseline; it is not, by itself, proof of authorship. The evidence used to
reconstruct it (raw session records) is private, is NOT part of this
repository, and must not be committed here.
