# Generic Bazel visual reviews for pull requests

## Current state

The first Haku-specific path is live:

- successful pull-request `CI` runs trigger the trusted
  `Publish Haku PR visuals` `workflow_run` workflow;
- `//haku/console/frontend:screenshots` emits PNGs and
  `haku-console-visuals.json` as undeclared test outputs;
- `devinfra/ci/pr_visuals.py` downloads those outputs through BuildBuddy,
  publishes an immutable page below `commits/<full-sha>/haku-console/`, and
  creates or updates one sticky pull-request comment;
- the page and PNGs are anonymously readable from the `pr-visuals` SeaweedFS
  S3 bucket.

This was exercised end to end on PR #3087 at commit
`85bae8b57326e08245da8dff24948c3bc7735509`: CI ran the screenshot test, the
trusted publisher uploaded eight PNGs and an HTML page, GitHub received the
sticky comment, and anonymous requests returned the page and image bytes.

The current implementation is intentionally narrow. The next version should
discover visual-producing Bazel tests generically, compare their outputs with
`devel`, and provide compact review navigation at commit, test, and asset
levels.

## Second consumer: AI quota

`//aiquota/gnome:test_render` is the motivating second producer. It renders five
GNOME Shell fixtures (`empty`, `extra_enabled_not_burning`, `hot`,
`stale_fallback`, and `tints`) and compares them with checked-in PNG goldens.
Today it writes actual, expected, and diff images to undeclared outputs only
when a comparison fails. That is useful for diagnosing a red test, but a
successful pull request provides no visual-review artifacts.

As part of the generic rollout, make this test always publish its five
candidate PNGs plus one `visual-review.json` manifest. Successful `devel` runs
then establish baselines, while affected pull-request runs become candidates.
The existing golden assertion remains the test's merge gate; the visual-review
publisher adds review evidence and does not replace or weaken that assertion.

Haku and AI quota exercise meaningfully different producer shapes:

- Haku emits a tall gallery of application UI states from a browser renderer;
- AI quota emits several independently named GNOME fixtures from a
  parametrized Docker test and already has a noise-tolerant PNG comparator.

The first generic implementation is not complete until both targets publish
through the same manifest and discovery path with no component-specific
publisher configuration.

## Desired contract

A Bazel test opts into visual review by writing a versioned manifest named
`visual-review.json` into its undeclared outputs directory. The publisher must
not contain component names, target allowlists, screenshot filenames, or
component-specific rendering logic.

The manifest describes presentation; BuildBuddy test-result metadata remains
the source of truth for the owning Bazel target.

```json
{
  "schema": "ducktape.visual-review.v1",
  "title": "Haku Console",
  "assets": [
    {
      "path": "previews-light.png",
      "label": "Tool previews - light",
      "mediaType": "image/png",
      "commentPriority": 100,
      "intensityThreshold": 0,
      "changeTolerance": 0.0
    }
  ]
}
```

Version 1 supports safe PNG basenames only. Reject absolute paths, traversal,
duplicate paths, unknown schema versions, missing files, invalid media types,
and unreasonable file/count/dimension limits. `commentPriority` is optional;
it selects useful inline previews without making alphabetical filename order a
UI contract. `intensityThreshold` and `changeTolerance` are optional and
default to exact comparison. A producer may set them only when its existing
visual test already documents and verifies an accepted rendering-noise model;
AI quota should reuse the semantics of its current threshold-16, two-percent
golden comparison.

## Discovery

The publisher runs only after successful pull-request CI and executes trusted
code from `devel`, preserving the existing secret boundary. PR code may
produce inert test outputs but never receives S3 credentials or the writable
GitHub token.

For every BuildBuddy test invocation recorded in the CI linkage:

1. enumerate test results actually present in the invocation, including
   remotely cached results;
2. associate each result with its canonical Bazel test label;
3. inspect its undeclared outputs for `visual-review.json`;
4. download and validate the manifest and declared assets;
5. group multiple shards or attempts under one test target, accepting only the
   final successful result and rejecting ambiguous conflicting manifests.

Do not recompute `bazel-diff` or infer ownership from changed paths. Actual test
results already embody the affected-target decision made by `bazel-ci`. An
affected test that produces no manifest simply contributes no visual review.

The current `bbapi artifact list` command is invocation-oriented and requires
probing several linked invocations. Extend `bbapi` if necessary so the Python
publisher can obtain test label, result identity, undeclared-output names, and
download locators in one bounded query rather than one API call per test.

## Baselines

The baseline for a pull-request asset is the newest successfully published
`devel` result for the same repository, canonical Bazel test label, and logical
asset path.

Publish visual manifests from successful `devel` CI runs into the same bucket,
without creating a PR comment. Maintain a small mutable baseline pointer per
test target that names an immutable commit SHA; publish immutable commit data
first and update the pointer last.

Baseline lookup outcomes are:

- baseline and candidate exist: `modified` or `unchanged`;
- candidate exists without a baseline: `new`;
- baseline exists but the candidate manifest omits it: `removed`;
- neither exists: impossible and treated as publisher corruption.

Asset identity is `(test label, manifest asset path)`. Display labels may
change without turning an asset into delete-plus-add. A later schema can add an
explicit stable asset ID if path renames need first-class tracking.

Record both baseline and candidate commit SHAs in generated metadata. Never
silently compare against a baseline from a different test target or repository.

## Image comparison

For PNG assets:

1. decode both images to a deterministic RGBA representation;
2. retain original dimensions for display and metadata;
3. compare on a transparent canvas large enough for both images;
4. produce a diff PNG that makes changed pixels obvious while retaining enough
   context to locate the change;
5. record changed-pixel count and percentage, dimensions, and classification;
6. classify byte-different but pixel-identical PNGs as unchanged.

Dimension changes are visual changes and must be called out explicitly. Exact
comparison is the default. Thresholding is producer-declared rather than a
publisher-wide guess and requires fixtures demonstrating the accepted noise;
AI quota's existing comparator supplies the initial non-zero-threshold case.

Count changes primarily at the asset level (`5 of 18 visuals changed`). Pixel
counts and percentages are per-asset diagnostic details, not the headline.

## Published structure

Use full commit SHAs and canonical test ownership. Normalize Bazel labels into
a reversible, collision-resistant URL segment; store the original label in
metadata and page copy.

```text
commits/<sha>/index.html
commits/<sha>/metadata.json
commits/<sha>/tests/<normalized-test>/index.html
commits/<sha>/tests/<normalized-test>/metadata.json
commits/<sha>/tests/<normalized-test>/assets/<normalized-asset>/index.html
commits/<sha>/tests/<normalized-test>/assets/<normalized-asset>/baseline.png
commits/<sha>/tests/<normalized-test>/assets/<normalized-asset>/candidate.png
commits/<sha>/tests/<normalized-test>/assets/<normalized-asset>/diff.png
```

The commit page groups results by test target, defaults to changed targets and
assets, and collapses unchanged results. It reports producing targets, changed
targets, total assets, modified/new/removed counts, and baseline coverage.

Each test page shows that target's summary and ordered asset list. Each asset
page is directly linkable and renders:

- baseline and candidate side by side when both exist;
- the candidate alone with `New visual` when no baseline exists;
- the baseline alone with `Removed visual` when no candidate exists;
- the generated diff and metrics for modified assets;
- baseline and candidate commit links and original dimensions.

All pages use shared Jinja templates and shared CSS assets. Upload leaf assets
first, then asset pages, test pages, and the commit index last so readers never
observe an advertised partial tree.

## Pull-request comment

Maintain one sticky comment for the newest successfully published commit. Use
an explicit GitHub commit link rather than a backticked SHA, which GitHub does
not autolink reliably:

```markdown
## Visual review for [`85bae8b`](https://github.com/agentydragon/ducktape/commit/85bae8b57326e08245da8dff24948c3bc7735509)
```

The comment contains:

- a link from the headline to the commit review page;
- producing-target and changed-target counts;
- modified, new, removed, and total asset counts;
- one section per changed test target, with the heading linked to its test
  page;
- at most two inline images across the entire comment, selected first by
  `commentPriority` and then by changed-pixel percentage;
- each image and asset label linked directly to its asset page;
- compact `N more diffs` and `N unchanged` links instead of listing every
  file.

Embed the generated diff for modified assets and the candidate for new assets.
For removals, embed the baseline with an unmistakable removed label. Keep the
comment below a fixed byte budget and remain useful when GitHub cannot proxy an
image.

When tests produced manifests but no assets changed, update the comment with a
short `No visual changes across N assets` result and retain the commit-page
link. When no executed test produced a manifest, do not create a new comment;
if a sticky comment from an older PR revision exists, update it to state that
the newest successful CI run produced no visual-review tests so it cannot be
mistaken for the current revision.

## Workflow behavior

Keep `workflow_run` as the trust boundary. GitHub will instantiate the workflow
after every successful PR `CI` run because affected-test metadata is available
only after downloading CI outputs. The job should gate cheaply:

1. download CI linkage;
2. discover whether any executed test exposes a visual manifest;
3. exit before S3 and comment work when none do;
4. otherwise resolve baselines, render, upload, and update the comment.

Do not introduce a PR-authored dispatch event solely to avoid the small empty
workflow run. Do not move writable S3 or PR-comment credentials into the
pull-request workflow.

Add concurrency keyed by PR number, cancelling older publisher runs. Before
updating the sticky comment, verify that the published SHA is still the PR's
current head; immutable pages for superseded SHAs may remain available.

## Implementation phases

### 1. Generic producer and discovery contract

- Define Pydantic models for `ducktape.visual-review.v1`.
- Change the Haku screenshot test to emit the generic manifest.
- Change `//aiquota/gnome:test_render` to retain all five successful candidate
  renders and emit one generic manifest after its parametrized cases complete.
- Add or extend `bbapi` commands to enumerate successful test results and
  undeclared outputs with canonical target labels.
- Replace Haku-specific publisher arguments and artifact names with generic
  discovery.
- Generate a commit page grouped by test target, initially without baselines.

Exit criterion: Haku and AI quota both publish through generic discovery, and
one affected CI run renders their separate Bazel target groups without
workflow or publisher configuration for either target.

### 2. Per-test and per-asset navigation

- Establish reversible target/asset URL normalization.
- Generate commit, test, and asset metadata plus Jinja pages.
- Deep-link comment target headings and asset previews.
- Add bounded inline comment previews and an explicit linked short commit SHA.

Exit criterion: every item visible in the PR comment has a useful direct page,
and dozens of assets do not produce a dozens-link comment.

### 3. Baseline publication and visual diffs

- Publish successful `devel` visual results without comments.
- Add per-target baseline pointers and immutable baseline provenance.
- Implement deterministic PNG decode, classification, diff rendering, and
  metrics.
- Render side-by-side baseline/candidate comparisons and new/removed states.

Exit criterion: fixtures cover unchanged, modified, new, removed, and
dimension-changed assets; a real PR shows the expected baseline and diff.

### 4. Operational hardening and repository-wide adoption

- Add size, count, dimension, timeout, and comment-budget limits.
- Add retry/error reporting for BuildBuddy and S3 without publishing partial
  indexes.
- Add PR-head freshness checks and per-PR workflow concurrency.
- Add retention policy separately from correctness; immutable commit pages must
  not disappear while referenced by an open PR.
- Document the producer recipe and add visual manifests to other Bazel UI or
  generated-artifact tests incrementally.

Exit criterion: another repository component opts in using only its Bazel test
and manifest, with no workflow or publisher code change.

## Verification

Unit and snapshot fixtures must cover:

- manifest validation and unsafe paths;
- multiple linked invocations, cached tests, shards, attempts, and duplicate
  target results;
- collision-free URL normalization;
- baseline lookup and provenance;
- unchanged, modified, new, removed, and dimension-changed PNGs;
- AI quota's five-asset parametrized manifest and its existing rendering-noise
  threshold semantics;
- upload ordering and content types;
- comment byte budgeting, priority ordering, commit links, and deep links;
- stale publisher runs that must not replace a newer PR comment.

End-to-end acceptance requires a disposable PR that affects both
`//haku/console/frontend:screenshots` and `//aiquota/gnome:test_render`. Verify
the GitHub comment, aggregate page, separate target pages, per-asset pages,
anonymous baseline/candidate/diff image access, and a subsequent revision that
updates the sticky comment rather than adding another.

## Non-goals

- Running every visual test on every PR; `bazel-diff` remains the affected-set
  source of truth.
- A paid external visual-regression service.
- Storing generated review pages or images in Git branches.
- Executing PR-controlled publisher or template code with trusted credentials.
- Making pixel diffs a required merge gate in the first iteration. Reviews are
  evidence first; gating can be considered after determinism is demonstrated.

## Open decisions

1. Exact `bbapi` API shape for enumerating test results and undeclared outputs.
2. Reversible normalized-label encoding versus a readable slug plus short hash.
3. Diff visualization style and whether a later schema needs per-asset masks.
4. Baseline pointer representation and retention period.
5. Whether unchanged-only runs should always maintain a short sticky comment or
   only update an already-existing one.
