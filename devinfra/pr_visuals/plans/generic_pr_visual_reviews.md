# Generic Bazel visual reviews for pull requests

## Goal

Make visual evidence from any affected Bazel test easy to review in a pull
request. The current collector accepts framework-neutral structured output
records, and the publisher compares screenshot roles by structured logical
identity. See <../README.md> for the current protocol and storage layout.
The remaining comparison work is the optional tolerance/noise-model path on
top of the exact default.

## Tolerance-aware comparison

Exact comparison is the shipped default. Add an optional per-asset noise model
so a producer whose visual test already documents sub-pixel drift (font
rasterization, anti-aliasing) can declare it instead of flagging every run:

- possible record fields `intensityThreshold` (per-channel max-diff gate) and
  `changeTolerance` (fraction of pixels that may differ) on visual asset
  records, with shared parity fixtures for the supported writers;
- the publisher honors them when classifying `unchanged` vs `modified` (today
  it compares exact decoded RGB via `devinfra/pr_visuals/visual_diff.compare_pngs`);
- AI quota (`//aiquota/gnome:test_render`) becomes the first declarer, matching
  its existing golden comparator (`tolerance=0.02`, `intensity_threshold=16`);
- `commentPriority` on assets so preview selection ranks by producer intent,
  not only changed-pixel percentage.

Exit criterion: a thresholded producer's no-op runs classify `unchanged` while
real changes still surface `modified`, with writer parity and fixtures covering
threshold-tolerated assets.

## Harden publication

- Extend output validation with file counts, file sizes, decoded
  dimensions, and total decoded-pixel limits.
- Bound BuildBuddy queries, downloads, image decoding, rendering, uploads, and
  GitHub comment size.
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

## Verification matrix

Automated coverage must include:

- record validation and unsafe paths;
- writer parity;
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

1. Diff visualization style and whether a later schema needs per-asset masks.
2. Whether unchanged-only runs always maintain a short sticky comment or only
   update an existing comment.
3. How a future tolerance policy should be represented without confusing
   screenshot identity with comparison settings.
