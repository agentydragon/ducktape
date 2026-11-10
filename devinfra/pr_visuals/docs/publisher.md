# PR visuals publisher internals

Read when changing `publisher.py`, `pull_request.py`, the publish workflow, or the
bucket layout. Overview: <../README.md>.

## Finding the invocation

The publisher asks BuildBuddy which CI test invocation the **commit** has, preferring
the `//...` devel sweep — a commit can have both a sweep and an affected-set run, and
the sweep can cover more visual tests, so the run that triggered the publish is
often not the one that ran the tests. Fallback: IDs derived from the run's identity
(<../../buildbuddy/ci/invocation_ids.py>), the only handle on a PR run (which records the merge
SHA, not the head SHA).

**Gotcha: artifacts come from the CAS, not `bbapi artifact download`.** That command
refetches the invocation's whole build event stream (33 MB on a sweep) per file,
~4.4 s per artifact (~16 min for a commit's ~294 files). The artifact listing already
carries each blob's `bytestream://` URI; the publisher passes exact destinations to
`bbapi blob download` in two manifest calls, one for output records and one for their
selected screenshot payloads. Each call downloads up to eight blobs concurrently.
The publisher logs phase starts and workload counts; Actions timestamps on those
lines show the time spent listing, downloading, building, and uploading.

The publisher also reads one bounded snapshot of output record JSON from the
private `pr-visuals-record-cache` bucket. Every run still lists BuildBuddy artifacts
and validates result identities, statuses, summaries, retries, record schemas, and
payload references. Only record bodies whose SHA-256 and size match a newly listed
CAS URI are reused; misses are downloaded in the usual manifest call. The snapshot
has limits on compressed size, expanded size, record count, and per-record size,
and is parsed in memory without extracting paths. Corrupt entries, missing buckets,
access failures, and failed writes log a warning and fall back to BuildBuddy.

Only a complete devel record collection updates the snapshot, merging its records
with older entries under a fixed size bound. The workflow sets
`PR_VISUALS_RECORD_CACHE_WRITE=1` only for devel push full sweeps. PR publications
read it without writing.
Concurrent devel writers can replace each other's snapshot and reduce future hit
rates; content verification and fresh result validation prevent a wrong output.
The workflow sets `PR_VISUALS_RECORD_CACHE_BUCKET`; the old pinned wheel ignores this
environment variable, while a new wheel reads it as the default for
`--record-cache-bucket`. Cache errors are best effort while the bucket is being
provisioned. The cache becomes active only after the private bucket is Ready and
the updated publisher wheel pin reaches `devel`; confirm both in the corresponding
rollout PRs. Record JSON may contain non-visual metadata. The publisher rejects configuring the
cache to use the anonymously readable review bucket; the workflow uses the separate
private bucket and never puts record bodies in an Actions cache shared with fork
workflows.

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
