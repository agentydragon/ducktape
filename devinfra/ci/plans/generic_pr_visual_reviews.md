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

The next milestone is useful visual comparison, not broader producer coverage:
for each candidate asset on a pull request, resolve its baseline from the PR's
base commit and show reviewers the baseline, candidate, and diff with clear
provenance.

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

## 2. Classify and render visual changes

For each PNG asset:

1. decode baseline and candidate into deterministic RGBA pixels;
2. retain original dimensions in metadata;
3. compare them on a transparent canvas large enough for both images;
4. generate a diff PNG that preserves enough context to locate changes;
5. record changed-pixel count, percentage, dimensions, and classification.

Classifications are:

- `unchanged`: decoded pixels match, even if encoded bytes differ;
- `modified`: baseline and candidate exist and exceed the declared tolerance;
- `new`: candidate exists without a baseline asset;
- `removed`: the baseline manifest contains an asset omitted by the candidate.

Dimension changes are always reported explicitly. Exact comparison remains the
default. A producer may declare `intensityThreshold` and `changeTolerance` only
when its own visual test already documents and verifies that noise model; AI
quota supplies the first thresholded case.

Each asset page renders baseline and candidate side by side when available,
the generated diff for modified assets, the appropriate single image for new
or removed assets, and links to both source commits. Test and commit pages
default to changed assets and collapse unchanged results.

The pull-request comment reports producing-target and changed-target counts,
plus modified, new, removed, and total asset counts. Embed at most two changed
previews across the entire comment, selected by `commentPriority` and then
changed-pixel percentage. Keep the comment useful within a fixed byte budget.

Exit criterion: fixtures cover unchanged, modified, new, removed, dimension-
changed, and threshold-tolerated assets, and a real pull request shows correct
side-by-side comparisons and diffs.

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
