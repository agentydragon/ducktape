# PR visuals publisher internals

Read when changing `publisher.py`, `pull_request.py`, the publish workflow, or the
bucket layout. Overview: <../README.md>.

## Selecting the invocation

The triggering CI run's test and build IDs are derived from its run ID and attempt
(<../../buildbuddy/ci/invocation_ids.py>). The publisher uses the triggering run's
test invocation for visual artifacts and both its test and build invocations for
failure diagnostics. PR CI can select only affected tests, so its review covers those
tests, even if a separate full sweep exists for the same commit. The ID is known
before Bazel starts; it does not guarantee that the invocation was created.

**Gotcha: artifacts come from the CAS, not `bbapi artifact download`.** That command
refetches the invocation's whole build event stream (33 MB on a sweep) per file,
~4.4 s per artifact (~16 min for a commit's ~294 files). The artifact listing already
carries each blob's `bytestream://` URI; the publisher passes exact destinations to
`bbapi blob download` in two manifest calls, one for output records and one for their
selected screenshot payloads. Each call downloads up to eight blobs concurrently.
The publisher logs phase starts and workload counts; Actions timestamps on those
lines show the time spent listing, downloading, building, and uploading.

## Which PR a run is for

Never taken from the run's event: `pull_requests` is empty for forks and resolved at
event delivery, not for the run's commit. The workflow passes a PR run's
`OWNER:BRANCH` as `--pull-request-head`; <../pull_request.py> finds the open PR whose
head is the run's commit and takes its number and base commit. A devel push passes
none, which is what lets it advance baseline pointers.

- Commit no longer an open PR's head (pushed over or closed before publication) →
  `neutral` check, no comment or bundle; the newer head's run publishes. Two matching
  PRs fail the run rather than guess.
- An open or merged PR receives a review only for its exact final head SHA;
  publication may finish after merge. The publisher rechecks the PR before updating
  the singleton comment, so a push mid-processing can't replace a newer review with
  an older one.
- **Deviation:** an agent-fork PR's Bazel run is `Trusted fork PR CI`
  (`pull_request_target`), not `CI`. Only the agent fork is followed: approving
  another fork's CI run doesn't extend to hosting its output in our bucket and
  comments.

## Cancelled runs

A cancelled upstream CI run cancels any running visual workflow for that same
run and attempt. The cancellation job completes an announced PR visual check as
`cancelled`; the check's external ID includes both run ID and attempt so a delayed
completion cannot overwrite a rerun's check. The job has no BuildBuddy or S3
credential. It does not wait for a remote invocation or publish artifacts
already produced by one. An exact-commit bundle from such a run may therefore
be absent; per-target baseline resolution falls back to the retained pointer
and marks the review `baseline_fallback`. A direct
`pr-visuals-publish --ci-conclusion cancelled` invocation likewise closes its
check without reading BuildBuddy or S3. A later announcement for the same
attempt leaves an already-completed check terminal.

Completed CI runs, including runs whose tests failed, still wait for a confirmed
complete test invocation before listing artifacts. Only their complete bundles
can advance devel baseline pointers. A successful CI workflow with no test
invocation has no visual bundle; `bazel_ci.py` skips `bazel test` when no tests
are selected.

## Baseline resolution

Per target, first check `reviews-test-outputs/<base_sha>/tests/<slug>/metadata.json`,
then the mutable `baselines/reviews-test-outputs/<slug>.json` pointer. Only complete,
stable targets are eligible. If neither exists, screenshots are new. The new namespace
intentionally does not read filename-based baselines; devel must publish fresh ones
after the cutover. Clear the old bundle and pointer namespaces as part of the
hard cutover; historical pages in those namespaces will no longer be available.

The `bazel-configuration-exact-v1` comparison profile allows comparisons only for
one known, equal configuration ID on both sides. A missing, changed, or ambiguous
configuration is shown as non-comparable. The opaque ID is execution provenance,
not part of a screenshot's identity; this conservative profile can reject two
semantically equivalent configurations.

Metadata retains structured case ID, optional checkpoint, role, and the result
provenance of every image variant. The collector selects confirmed successful final
attempts, records retries, and reports missing results, failed results, and collisions.
Different images from repeated runs are shown as unstable, with each variant and its
provenance. An incomplete or unstable target never advances its devel pointer and
cannot establish that a screenshot was removed. Pointer-resolved comparisons are
labelled stale because their changes can include devel drift.

Discovery starts with targets that expose at least one output record. A target
that publishes no records at all cannot be identified as a visual producer from
the artifact listing alone. The determinism sweep supplies an explicit expected
target list; consumers that require that stronger coverage should do the same.

Candidate, baseline, and diff PNGs use shared SHA-256-addressed blobs. A matching
candidate digest skips the baseline image read; otherwise the publisher reads and
verifies the referenced image before comparing pixels. A missing or corrupted
baseline image is an error, not evidence of a new screenshot. Pointers advance only
after the review bundle and shared images upload successfully.

## Check-runs

- **PR visual review**: `in_progress` with the Bazel CI run (linking it); then
  `success` once bundle and comment are done, `failure` on invalid producer output or
  publisher errors, `neutral` when no test exposed screenshot records. Failed CI runs still
  publish what arrived; summary and comment list failed targets when BuildBuddy
  exposes them. Reviewers can request a manual rerun from the CI run page.
- **PR visual diffs**: only when a baseline comparison ran; `success` if nothing was
  modified, removed, unstable or incomplete, else `neutral` — deliberately not a merge gate.

After publication, the publisher rechecks the PR before changing its singleton
comment. A failed recheck or comment update marks the final visual-review check
`failure`; the publisher still attempts that check update. If publication itself
failed, its original error remains the command's error, with any reporting errors
attached as its cause. A stale or closed PR never receives a comment; a PR
already stale at the initial lookup gets a `neutral` check.

The singleton bot comment reports the latest result. A published bundle, CI
failure, or processing failure creates or updates it. A successful run with no
visual output updates an existing comment to describe that run, but creates no
comment when none exists. This can happen for the same PR head when the affected
test set changes, so an earlier review is not preserved as the current result.
Every newly written comment starts with one versioned HTML marker,
`<!-- pr-visuals {"version":1,...} -->`, containing its commit SHA, result
kind, and CI conclusion. Older comments using `<!-- pr-visuals -->` remain
discoverable and are replaced without parsing their display text.

## Storage

- Each distinct PNG is stored once at `assets/<sha256>.png`, shared across targets,
  commits, and candidate/Before/diff roles; review pages, styles, scripts, and
  metadata at `reviews-test-outputs/<sha>/`.
- Missing images are uploaded with `PutObject` + `If-None-Match: *`, so concurrent
  publishers can't replace a shared image. Shared-image existence checks and uploads
  run with a fixed worker limit; all finish before review metadata or pages upload.
  Order: every referenced image, then review metadata and pages, then the aggregate
  gallery index last. A failed image upload blocks review publication and pointer
  advancement.
- Existing commit bundles are not migrated.

## Released commands

`//devinfra/pr_visuals:wheel` builds the `pr-visuals` wheel (`pr-visuals-announce`
for check-runs, `pr-visuals-publish` for publication), bundling the publisher's small
internal source deps and HTML templates; Python runtime deps are wheel metadata.

Both workflow jobs install `.#pr-visuals` from trusted `devel` (wheel deps + `bbapi`;
closure in the public Attic cache) and log the wheel URL and hash from
`nix/artifact-pins.json` first. Source changes ship via the content-addressed GitHub
release pipeline, then `sync-pins` bumps the wheel pin; `flake.lock` pins the Nix
deps. The Nix wheel CI gate substitutes the PR-built wheel for the pin and checks
imports and both entrypoints without the checkout.
