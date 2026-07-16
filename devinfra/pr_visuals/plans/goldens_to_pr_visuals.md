# Replace checked-in screenshot goldens with PR visual reviews

## Goal

Stop storing rendered-screenshot goldens in git. The PR-visuals pipeline
(<../README.md>) already performs an S3-backed golden comparison — baseline
resolution, per-asset classification, diff overlays, PR comment, and the
`PR visual diffs` check-run — so the git-stored PNGs and their
CI-enforced pixel asserts are redundant weight: render PNGs don't delta,
and every UI tweak re-adds megabytes to permanent history.

The behavioral half of those tests stays a hard gate. Today's "golden" tests
are two tests glued together: a behavioral one (server boots, page renders,
load-bearing DOM appears, no JS errors, deterministic double-render settles)
and a pixel one (did anything change). Keep the first as hard CI fail; move
only the second to human review via PR visuals.

## Target model

| Tier          | Mechanism                                                                                                              | Gate         |
| ------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------ |
| Behavior e2e  | Playwright DOM/behavior asserts (`test_e2e_browser`, augur `wait_ready` chart-geometry checks)                         | Hard CI fail |
| Render health | Same visual tests minus the golden compare: real server/harness, load-bearing DOM waits, double-render, no page errors | Hard CI fail |
| Pixel changes | `retain_review_asset` / manifest emission → publisher diffs vs baseline → PR comment + `PR visual diffs` check         | Human review |

Haku's preview screenshots already work publish-only; this converges the rest
of the repo on that model.

## Inventory (verified 2026-07-16)

Checked-in render goldens at HEAD: 13 MB across these directories, with
28 MB of distinct PNG blobs already in full history. (`props/specimens/**`
copies are frozen eval-corpus fixtures — out of scope.)

Python path — `util/testing/png_diff.py::assert_png_matches_golden`,
regenerated via `UPDATE_GOLDEN`:

| Test                                         | Golden dir                                | Size   |
| -------------------------------------------- | ----------------------------------------- | ------ |
| `x/study_casino/tests/visual_golden_test.py` | `x/study_casino/frontend/__screenshots__` | 5.0 MB |
| `finance/augur/visual_test.py`               | `finance/augur/frontend/__screenshots__`  | 3.3 MB |
| `aiquota/gnome/test_render.py`               | `aiquota/gnome/__snapshots__`             | 164 KB |

JS path — `util/testing/frontend_visual/visual-test-lib.mjs::compareBaseline`,
regenerated via `--update` / `UPDATE_BASELINES=1`:

| Tests                                             | Golden dir                                                         | Size    |
| ------------------------------------------------- | ------------------------------------------------------------------ | ------- |
| `x/study_casino/frontend/tests/visual_test_*.mjs` | `x/study_casino/frontend/tests/baselines`                          | 2.6 MB  |
| `props/frontend/src/**/visual_test_*.mjs`         | `props/frontend/src/{components,components/stats,pages}/baselines` | ~750 KB |
| `airlock/frontend/tests/visual_test_runner.mjs`   | `airlock/frontend/baselines`                                       | 408 KB  |

## Phased execution

### PR 1 — publisher trustworthy enough to replace a gate ✅ done (#3278)

Baseline-chain fix (mutable `baselines/<slug>.json` pointers bridging
cache-hit gaps in devel bundles), the `PR visual diffs` check-run (neutral on
modified/removed — visible in the checks list so a skimmed comment isn't the
only defense), retention decision (keep everything; bundles immutable,
pointers forward-only), and docs.

### PR 2 — pilot on study casino ✅ done (#3289)

- Convert `visual_golden_test.py` to render-health + publish: delete the
  golden assert, the `UPDATE_GOLDEN` flow, and `frontend/__screenshots__/`.
  Keep the server boot, DOM waits, determinism double-render, and
  page-error failure.
- Add a publish-only mode to `visual-test-lib.mjs` for the harness scenario
  tests; delete `frontend/tests/baselines/`.
- Backfill the behavior gaps the pixel asserts quietly covered: DOM e2e for
  the changelog-ack flow and the award toast after stop-and-save.

### PR 3 — fleet conversion

- Convert augur (keep its `wait_ready` asserts — they're the real regression
  net), props, airlock, and aiquota gnome; delete their golden dirs.
- Delete the `compareBaseline` path from `visual-test-lib.mjs` (no consumers
  left) and `assert_png_matches_golden` from `png_diff.py` if nothing else
  uses it.
- Extract the duplicated uvicorn-server/double-render machinery shared by
  `x/study_casino/tests/visual_golden_test.py` and `finance/augur/visual_test.py`
  into `util/testing`.

### PR 4 — policy + guardrail

- Document the model in testing docs: no checked-in render PNGs; pixels are
  reviewed via PR visuals; behavior is asserted in DOM.
- Pre-commit check rejecting new PNGs under `**/__screenshots__/`,
  `**/__snapshots__/`, and `**/baselines/` (allow-listing genuine input
  fixtures, e.g. `props/specimens/**`).

## Decisions

- **Soft gate**: pixel diffs conclude the `PR visual diffs` check `neutral`,
  never `failure`. A hard gate (fail unless an acknowledging PR label is
  present) is easy to add later if review discipline slips.
- **No history purge**: the ~28 MB already in history isn't worth a
  clone-breaking `git filter-repo` rewrite; policy stops the growth.
- **No exceptions expected**: no current test needs a hard pixel gate once
  render-health + diffs-in-review exist. If one emerges, a per-target golden
  remains possible as a documented exception.

## Non-goals

- Weakening behavioral coverage — every DOM/behavior assert embedded in
  today's visual tests survives conversion.
- Changing which tests run per PR; Bazel's affected-target selection remains
  the source of truth.
