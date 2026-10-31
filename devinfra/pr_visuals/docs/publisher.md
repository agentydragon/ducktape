# PR visuals publisher internals

Read when changing `publisher.py`, `pull_request.py`, the publish workflow, or the
bucket layout. Overview: <../README.md>.

## Finding the invocation

The publisher asks BuildBuddy which CI test invocation the **commit** has, preferring
the `//...` devel sweep — a commit can have both a sweep and an affected-set run, and
only the sweep carries visual manifests, so the run that triggered the publish is
often not the one that ran the tests. Fallback: IDs derived from the run's identity
(<../../buildbuddy/ci/invocation_ids.py>), the only handle on a PR run (which records the merge
SHA, not the head SHA) or a run cancelled before Bazel started.

**Gotcha: artifacts come from the CAS, not `bbapi artifact download`.** That command
refetches the invocation's whole build event stream (33 MB on a sweep) per file,
~4.4 s per artifact (~16 min for a commit's ~294 files). The artifact listing already
carries each blob's `bytestream://` URI; the publisher passes exact destinations to
`bbapi blob download` in two manifest calls, one for review manifests and one for their
assets. Each call downloads up to eight blobs concurrently (<20 s for a commit's files).

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

## Superseded runs

A run cancelled as superseded waits for its known BuildBuddy test invocation to
reach `COMPLETE_INVOCATION_STATUS` before listing artifacts, then publishes its
immutable commit bundle and posts nothing else (the superseding run brings the
real review). Cancelling the workflow can precede creation of the remote
invocation, or BuildBuddy can briefly report it as disconnected, so neither state
ends the wait. The wait is bounded to five minutes; after that, the cancelled run
may publish whatever artifacts have arrived but never advances baseline pointers.

Damage is bounded to **staleness, never wrongness**, by two properties to preserve:

- Bundles and baseline resolution are per-target, so a partial commit is not a broken
  baseline: missing targets resolve via the pointer, marked `baseline_fallback`.
- Only a confirmed complete invocation advances a pointer: `publish_only` never
  writes pointers, and the other path rejects incomplete invocations before
  listing artifacts. A successful CI workflow with no test invocation has no
  visual bundle; `bazel_ci.py` skips `bazel test` when no tests are selected.

If a cancelled invocation runs longer than the bound, the commit bundle may be
partial. Missing targets resolve via the pointer with a `baseline_fallback` warning.

## Baseline resolution

Per target, first hit wins:

1. `reviews/<base_sha>/tests/<slug>/` — the base commit's devel publication;
2. retained `commits/<base_sha>/tests/<slug>/` bundle;
3. `baselines/reviews/<slug>.json` — newest devel commit whose review bundle has the
   target;
4. retained pointer `baselines/<slug>.json`;
5. none → every asset is `new`.

- Metadata records SHA-256 of candidate, Before, and diff PNGs. When candidate bytes
  match the baseline's recorded candidate digest, classification skips the baseline
  PNG read and pixel decode; digest-less or differently encoded images retain the
  baseline-download and pixel-comparison path. Digest-less baselines resolve their
  candidate PNG from the retained commit bundle. The `reviews/` and
  `baselines/reviews/` prefixes isolate overlapping publisher jobs on different
  released wheels: old jobs keep the old layout, and old URLs stay valid.
- Devel pushes publish every re-executed target's artifacts even when an unrelated
  target fails (a flake elsewhere must not drop a valid result); a failed visual
  target with no manifest can't advance its pointer. Pointers advance after the
  commit bundle upload completes.
- Cache hits re-expose nothing, so a fully cached devel run (the norm after the PR's
  own CI tested the same tree) publishes nothing and advances no pointers; a pointer
  heals at the next devel run that re-executes its target (e.g. after an RBE image pin
  bump).
- Pointer-resolved targets are `baseline_fallback`: the comment warns differences may
  predate the PR, and each modified/removed screenshot is labelled `stale baseline`
  with the baseline commit and merge base (whether it differs at the merge base is
  unknowable — that bundle has no image for the target).

## Check-runs

- **PR visual review**: `in_progress` with the Bazel CI run (linking it); then
  `success` once bundle and comment are done, `failure` on invalid producer output or
  publisher errors, `neutral` when no test exposed a manifest. Failed CI runs still
  publish what arrived; summary and comment list failed targets when BuildBuddy
  exposes them. Reviewers can request a manual rerun from the CI run page.
- **PR visual diffs**: only when a baseline comparison ran; `success` if nothing was
  modified or removed, else `neutral` — deliberately not a merge gate.

## Storage

- Each distinct PNG is stored once at `assets/<sha256>.png`, shared across targets,
  commits, and candidate/Before/diff roles; review pages, styles, scripts, and
  metadata at `reviews/<sha>/`.
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
