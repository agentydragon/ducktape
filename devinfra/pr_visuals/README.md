# devinfra/pr_visuals

Trusted publisher for PR visual reviews. The "Publish PR visuals" workflow
(`.github/workflows/haku-pr-visuals-publish.yml`) runs `publisher.py` after
every successful Bazel CI run. It scans the run's test invocations for targets
whose undeclared outputs contain a `visual-review.json` manifest (schema:
`util/visual_review.py`), downloads the referenced PNGs, publishes an immutable
bundle to `s3.allegedly.works/pr-visuals`, and upserts a review comment on the
PR. Cache-hit test targets don't republish artifacts, so each bundle carries
only the visual tests the run invalidated.

## Opting a visual test in

Use one of the shared harnesses and it's automatic:

- JS (`js_test`): `util/testing/frontend_visual/visual-test-lib.mjs` retains
  the rendered PNG and upserts the manifest on every run.
- Python (`py_test`): call
  `util.testing.visual_review.retain_review_asset(png, title=..., label=...)`
  once per rendered case — it copies the PNG into undeclared outputs and
  accumulates the manifest.
- Custom drivers write the manifest themselves via
  `writeVisualReviewManifest` / `write_visual_review_manifest`
  (e.g. haku's `tool_rendering/screenshot/render.mjs`).

## Baseline resolution

PR runs compare each target's assets against a baseline bundle, resolved
per target:

1. `commits/<base_sha>/tests/<slug>/` — the PR's base commit, when that devel
   run published the target;
2. otherwise the mutable pointer `baselines/<slug>.json`, naming the newest
   devel commit whose immutable bundle carries the target.

Devel pushes only publish targets Bazel actually re-executed (cache hits
publish nothing), so base-commit bundles routinely lack untouched targets —
the pointer bridges those gaps. Devel-push publishes advance the pointers
after the commit bundle upload completes; targets resolved through a pointer
are marked `baseline_fallback` in the bundle metadata. If neither source
carries the target, every asset classifies as `new`.

## Check-runs

- **PR visual review** — starts `in_progress` with the Bazel CI run and links
  there while CI executes. The publisher updates that same check to `success`
  once the bundle is uploaded and the comment upserted, `failure` on invalid
  producer output or publisher errors, or `neutral` when no test exposed a
  manifest. If Bazel CI does not succeed, its conclusion completes the check
  without running the publisher.
- **PR visual diffs** — comparison outcome, present only when a baseline
  comparison ran: `success` when nothing was modified or removed, `neutral`
  otherwise. Neutral is deliberate — the check points reviewers at visual
  changes; it is not a merge gate.

## Retention

Nothing is deleted: commit bundles are immutable and pointer files only ever
advance. The bucket grows with devel history; garbage-collecting bundles that
no pointer or open PR references is future work
(<plans/generic_pr_visual_reviews.md>).
