# devinfra/pr_visuals

Trusted publisher for PR visual reviews. The "Publish PR visuals" workflow
(`.github/workflows/pr-visuals-publish.yml`) runs `publisher.py` after
every PR and `devel` Bazel CI run, including failed and superseded ones. It
locates that commit's Bazel invocations — by asking BuildBuddy which CI test
invocation the commit has, falling back to IDs derived from the run's identity
(<devinfra/ci/invocation_ids.py>) where it cannot know — then scans them for
targets whose undeclared outputs contain a
`visual-review.json` manifest (schema:
`util/visual_review.py`), downloads the referenced PNGs, publishes an immutable
bundle to `s3.allegedly.works/pr-visuals`, and upserts a review comment on the
PR. A cache-hit test still reports its undeclared outputs, so a bundle carries
every visual target the run covered rather than only the ones it re-executed —
which takes a download-mode flag to hold
(<devinfra/docs/bazel_caching.md> § Undeclared test outputs under BwoB).

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

**Gotcha: one commit, several CI runs.** A `//...` devel sweep and an affected-set
run can both exist at one commit, and only the sweep carries visual manifests. So
the invocation is found by _commit_, preferring the sweep, rather than by the run
that triggered this publish — that run is frequently not the one that ran the
tests. The by-run derivation remains the fallback: it is the only handle on a PR
run, which records the merge SHA and so cannot be found by head SHA, and on a run
cancelled before Bazel started.

**Gotcha: artifacts come from the CAS, not from `bbapi artifact download`.** That
command resolves its `label/name` pattern against the invocation's whole build event
stream, and refetches that stream — 33 MB on a `//...` sweep — for every file, so it
costs ~4.4 s per artifact whatever the artifact's size. At a commit's ~294 files the
publish step took ~16 minutes. The artifact listing already carries each blob's
`bytestream://` URI, so the publisher reads blobs straight from
`app.buildbuddy.io/file/download`, eight at a time: the same set lands in under 20
seconds.

A run cancelled as superseded publishes whatever visual artifacts have reached
BuildBuddy by then, and says nothing else: the PR comment is a singleton, and the
run that superseded it is already on its way with the real review.

**Gotcha: a superseded run's publish races its own Bazel invocation.** Cancelling
the workflow does not cancel the invocation, so the cancelled CI run reaches
`completed` at once while Bazel keeps streaming results for minutes afterwards.
The publish reads BuildBuddy at that moment and takes what is there — often
nothing, and nothing revisits the invocation once it finishes. Measured on
`29154c806`: its publish ran 14:29–14:31 and found no manifests; its sweep ran
14:26:40 → 14:32:27 and ended with all 34. That commit has no bundle.

The damage is bounded to **staleness, never wrongness**, by two properties worth
preserving:

- Bundles and baseline resolution are both per-target, so a partially published
  commit is not a broken baseline — each target it lacks resolves through the
  pointer instead, marked `baseline_fallback`.
- Only a complete invocation can advance a pointer. `publish_only` never writes
  pointers, and the other path's write is reachable only when the run was _not_
  cancelled — which means bazel-ci ran to completion, so its invocation is
  finished.

A chain of rapid devel merges therefore leaves baselines progressively older
(`bazel-ci.yml` cancels the superseded run; `ci.yml` puts that at ~8 in 10
pushes) and puts more `baseline_fallback` warnings on PR comments. The first
uncancelled run heals it. Deferred fix: <devinfra/ci/TODO.md> § Visual publishing
races the invocation it reads.

## Baseline resolution

PR runs compare each target's assets against a baseline bundle, resolved
per target:

1. `commits/<base_sha>/tests/<slug>/` — the PR's base commit, when that devel
   run published the target;
2. otherwise the mutable pointer `baselines/<slug>.json`, naming the newest
   devel commit whose immutable bundle carries the target.

Devel pushes publish the visual artifacts of every target the completed Bazel
run re-executed, even when an unrelated target fails; a failed visual target
that produces no manifest cannot advance its pointer. This keeps an
otherwise-valid visual result from being dropped solely because CI has a flake
elsewhere. Cache-hit tests re-expose nothing, so a fully cached devel run —
the norm when the merged PR's own CI already tested the identical tree —
skips publication and advances no pointers; a pointer heals at the first
devel run that re-executes its target, typically forced by the next
cache-busting commit (e.g. an rbe-worker image pin bump). Devel-push
publications advance the pointers after the commit bundle upload completes;
targets resolved through a pointer are marked `baseline_fallback` in the
bundle metadata and the PR comment warns that those differences may predate
the PR. If neither source carries the target, every asset classifies as `new`.

## Check-runs

- **PR visual review** — starts `in_progress` with the Bazel CI run and links
  there while CI executes. The publisher updates that same check to `success`
  once the bundle is uploaded and the comment upserted, `failure` on invalid
  producer output or publisher errors, or `neutral` when no test exposed a
  manifest. Failed CI runs still publish every visual artifact that arrived;
  the check summary and PR comment list failed Bazel targets when BuildBuddy
  exposes them.
- **PR visual diffs** — comparison outcome, present only when a baseline
  comparison ran: `success` when nothing was modified or removed, `neutral`
  otherwise. Neutral is deliberate — the check points reviewers at visual
  changes; it is not a merge gate.

## Retention

Nothing is deleted: commit bundles are immutable and pointer files only ever
advance. The bucket grows with devel history; garbage-collecting bundles that
no pointer or open PR references is future work
(<plans/generic_pr_visual_reviews.md>).
