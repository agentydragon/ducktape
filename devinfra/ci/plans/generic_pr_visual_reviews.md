# Generic Bazel visual reviews for pull requests

## Goal

Make visual evidence from any affected Bazel test easy to review in a pull
request. A producer opts in by writing `visual-review.json` and its declared
PNG assets as undeclared test outputs; the trusted publisher discovers those
outputs from the tests that Bazel CI actually executed.

Successful `devel` runs publish an immutable bundle per commit at
`commits/<sha>/` — the same commit-pinned paths PR runs use — so every devel
commit's visual output is addressable by SHA. Baseline identity is
`(repository, canonical Bazel test label, manifest asset path)`.

The baseline/diff path ships exact-pixel comparison: the publisher resolves each
candidate asset's baseline from the PR's base commit (passed by CI as
`--base-sha`), classifies it `unchanged` / `modified` / `new` / `removed`,
renders baseline↔candidate + diff, and reports counts with up to two diff
previews in the PR comment. The remaining comparison work is the optional
tolerance/noise-model path on top of that exact default.

## 1. Exercise the generic path live

Create a disposable pull request that affects both
`//haku/console/frontend:screenshots` and `//aiquota/gnome:test_render` without
changing publisher configuration.

Verify on its first revision:

- Bazel CI executes both visual tests and the publisher discovers both targets;
- one sticky comment links to the commit page and separate per-test pages;
- all declared PNGs are anonymously readable from the public bucket;
- the linked short SHA resolves to the tested commit;
- the `PR visual review` check succeeds.

Push a second revision and verify that the publisher updates the existing
sticky comment rather than adding another. Confirm that a superseded publisher
run cannot replace the comment for the newer head.

Exercise the error path with an invalid or incomplete manifest, then restore
it. The sticky comment must identify the affected test and error, the visual
review check must fail, and the workflow must exit non-zero without advertising
a partially uploaded index.

Exit criterion: a real pull request demonstrates two independently implemented
producers, sticky-comment maintenance across revisions, and actionable failure
reporting.

## 2. Tolerance-aware comparison

Exact comparison is the shipped default. Add an optional per-asset noise model
so a producer whose visual test already documents sub-pixel drift (font
rasterization, anti-aliasing) can declare it instead of flagging every run:

- manifest fields `intensityThreshold` (per-channel max-diff gate) and
  `changeTolerance` (fraction of pixels that may differ) on
  `ducktape.visual-review.v1` assets, added to both the Python and JavaScript
  writers with shared parity fixtures;
- the publisher honors them when classifying `unchanged` vs `modified` (today
  it compares exact decoded RGB via `util/visual_diff.compare_pngs`);
- AI quota (`//aiquota/gnome:test_render`) becomes the first declarer, matching
  its existing golden comparator (`tolerance=0.02`, `intensity_threshold=16`);
- `commentPriority` on assets so preview selection ranks by producer intent,
  not only changed-pixel percentage.

Exit criterion: a thresholded producer's no-op runs classify `unchanged` while
real changes still surface `modified`, with writer parity and fixtures covering
threshold-tolerated assets.

## 3. Harden publication

- Extend manifest validation with media types, file counts, file sizes, decoded
  dimensions, and total decoded-pixel limits.
- Bound BuildBuddy queries, downloads, image decoding, rendering, uploads, and
  GitHub comment size.
- Handle cached results, linked invocations, shards, retries, and duplicate
  target results. Accept only the final successful result and reject ambiguous
  conflicting manifests.
- Add per-pull-request workflow concurrency and cancel older runs.
- Recheck the pull request head immediately before replacing its sticky
  comment.
- Preserve leaf-first upload ordering as asset pages are added, keeping the
  commit index last.
- Add bounded retries for BuildBuddy, S3, and GitHub operations while retaining
  the existing sticky-comment and failing-check reporting.
- Emit enough structured workflow diagnostics to distinguish missing producer
  output from publisher corruption or an unavailable dependency.

Exit criterion: fault-injection tests prove that stale runs and partial
failures cannot publish a misleading current result.

## 4. Document and expand producer coverage

Write a short producer recipe next to the shared Python and JavaScript manifest
writers. A new producer should need only to:

1. write PNGs into its undeclared outputs directory;
2. write one `ducktape.visual-review.v1` manifest using a shared writer;
3. ensure its Bazel test is selected by the existing affected-target CI path.

Add another repository component only after the baseline/diff path and limits
are proven. It must opt in without changing workflow or publisher code.

Keep the Python and JavaScript writers behaviorally identical with shared JSON
fixtures. Reconsider maintaining both implementations once a third producer
makes the repository's dominant producer language clear.

Exit criterion: a third component publishes visual reviews with producer-only
changes and the recipe is sufficient for an unfamiliar contributor.

## Verification matrix

Automated coverage must include:

- manifest validation and unsafe paths;
- Python/JavaScript writer parity;
- multiple linked invocations, cached tests, shards, attempts, and conflicting
  duplicate results;
- collision-free target and asset URL normalization;
- baseline lookup and provenance isolation;
- unchanged, modified, new, removed, dimension-changed, and threshold-tolerated
  PNGs;
- upload ordering, content types, and interrupted uploads;
- comment budgeting, preview priority, commit links, and deep links;
- stale publisher runs and pull-request head changes;
- neutral no-producer runs and actionable producer/publisher failures.

End-to-end acceptance requires anonymous reads of every linked HTML page and
image, not merely a successful upload command.

## Non-goals

- Running every visual test on every pull request; Bazel CI's affected test set
  remains the source of truth.
- A paid external visual-regression service.
- Storing generated pages or images in Git branches.
- Executing pull-request-controlled publisher or template code with trusted
  credentials.
- Making pixel differences a required merge gate before determinism and the
  accepted noise models are demonstrated.

## Open decisions

1. Retention policy for superseded baseline commit bundles and PR-referenced
   pages.
2. Diff visualization style and whether a later schema needs per-asset masks.
3. Whether unchanged-only runs always maintain a short sticky comment or only
   update an existing comment.
4. Whether the Python and JavaScript manifest writers remain justified after
   repository-wide adoption begins.
