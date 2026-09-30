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

## Phased execution

### Remaining: policy + guardrail

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
