# How publishing decides what to publish

This is how CI identifies the release artifacts and images whose content changed.

## The rule

**Use the devel build to plan, then build only what a publish job needs.**
`bazel-ci` runs `bazel test //...` and `bazel build //...` on each devel push.
The planners read those invocation streams to skip items already published. A
release job rebuilds changed release assets; an image job builds its image
before pushing. For a literal workspace test label, the publish job can reuse
the `testSummary` from that same successful push-CI test invocation when its
source, Bazel configuration, RBE image, terminal status, and one-attempt result
all match. Otherwise the publication job runs its own tests as before.

BuildBuddy keeps that invocation's Build Event Protocol stream and serves it as
JSON. Each stream carries three things at once:

- every output file, with its content digest, path prefix and producing label;
- a `testSummary` verdict per test target;
- `bytestream://` URIs for fetching any of those files.

`devinfra/ci/bes.py` reads it. `plan_releases.py` and `plan_image_pushes.py`
consume it. `bazel-ci` publishes its invocation ids as a workflow output and
`ci.yml` passes them down. The test invocation also retains its lossless per-
configuration summaries and provenance; the merged output view is never used as
test-gate authority.

The reuse boundary is deliberately small. Only literal `//package:test` labels
with exactly one `PASSED` summary, a configuration id, and one attempt are
eligible. The CI stream must be the current push's deterministic test
invocation, carry the current checkout's `COMMIT_SHA`, request `//...`, finish
successfully, match the publication job's `--config=ci`, and report the
expected effective RBE executor settings and image. A cached one-attempt pass
is accepted because Bazel keys test results by source and configuration.
Missing or unreadable evidence, custom release flags, retries, multiple
configurations, failed summaries, suites,
external labels, wildcards, PR runs and dispatch runs preserve the publication
test step.

## Identity

A release's tag is `<pkg>-<first 12 of its content hash>`. For a single-asset
release that hash **is** the artifact's sha256, which the stream already
reports — so no bytes are fetched at all. Multi-asset releases compose the same
hash from the same digests via
`release_content_hash_from_digests`, byte-identically to hashing the files. It
has to stay byte-identical: a different hash republishes every package once,
under a new tag, and Flux chases each one.

An image is different. Its identity is the _contents_ of its
`<name>.json.sha256` file, not that file's own digest, so those ~70 bytes are
fetched. That is the only download in either planner.

The digest that decides is the **unstamped** one. `release-artifact` builds each
release twice: unstamped first, whose hash becomes the tag and the skip
decision, then stamped only if that tag is new, because stamping embeds the
commit and would make every build's bytes differ. `bazel-ci` does not pass
`--stamp` either, so the stream the planner reads reports exactly the digests
build 1 would have produced. That correspondence is what lets the planner skip
the build at all.

Outputs are located **by label** (`//pkg:image.digest`), never by deriving a
path. An external repository's directory name is mangled by bzlmod and cannot be
reconstructed, and most image targets here are literally named `image` — a path
or basename guess resolves to a different image's file, quietly. Where only a
path is available, it is `pathPrefix` + `name`: a source file and the generated
file of the same name differ only by prefix.

The publish side resolves the same way: `artifact_targets.json` names targets
and released filenames, never Bazel output paths, and after each build
`release_assets.py` reads that build's own stream to find where the assets
landed (`bes.artifact_output` — the one default-group, non-aspect file a label
reports; bazel-ci's lint aspects complete report files under the same label).
Unlike the planner it fails loud, because it runs in the job about to publish.

## The planner fails open; the push job does not

`release` and `push-images` run under `always() && !cancelled()`, so they run
even when `bazel-ci` failed or was skipped. **In the planner, anything short of
proof that an item is unchanged republishes it.** No invocation, an output the
stream never mentions, an unreadable blob, an unreachable registry — all keep the
item in the matrix.

The asymmetry is deliberate: wrongly including an item costs one job, wrongly
excluding one silently skips a deployment. A planner that cannot prove anything
must degrade to the full fan-out it replaced, never to publishing nothing.

What makes that cheap is that the item's own job decides again, from what it
built rather than from what the planner could read. A release is content-addressed
— same bytes, same tag, nothing new to publish. An image is not: every push mints
a fresh `devel-<timestamp>-<sha>` tag that Flux picks up, so publishing an
unchanged image still costs a commit, a reconcile and a rollout. So
`devinfra/ci/push_image.py` compares its built digest against what the registry
last published and pushes only on a difference. Before it was decommissioned and
removed from the image roster, `manifold-mcp-server` churned a deployment on every
merge because this sweep could not see its nested-module target.

**And there the asymmetry reverses.** The push job is the last check, so a
registry it cannot read fails the job rather than publishing regardless. Carrying
the planner's rule down here would have meant a blip republishing the very images
this check exists to hold back — and it would rarely buy anything, since the push
that follows goes to the same registry with the same credentials. A push not made
is recovered by the next merge, which still sees the difference; a push wrongly
made is a rollout.

## What a `//...` sweep cannot cover

Some rows are outside the sweep, and no amount of care here brings them in:

| Row           | Why                                                                        |
| ------------- | -------------------------------------------------------------------------- |
| `aw-importer` | `@ducktape_activitywatch//importer:…` — external repo; `//...` misses it   |
| `gterm-theme` | `tags = ["manual"]` (needs libgirepository, libdbus), so wildcards skip it |
| `debundle`    | builds under `-c opt`, a different configuration                           |

**Rows in that table are slow paths:** the default devel sweep cannot prove
their identity. They stay in the publish matrix, where their own jobs build and
check the required artifact before publishing.

Test selectors have a separate boundary: exact test labels may reuse verdicts;
suite selectors (including current `:all` and `:tests` rows), patterns, and
external labels stay on the publication test path because they have no matching
test summary of their own. A passing child label is not evidence that a selected
suite or pattern had complete coverage.

## Alternatives, and why not

**Rebuild in the plan job.** What this replaced. `bb remote build`'s post-run
download returns an arbitrary subset of the requested outputs and exits 0 — 12
files for 50 targets on devel `d4e6fe42`. The planners could not see what they
had built, so they fell through to fail-open for 38 of 50 rows and published
them anyway. See <../../debug/bb_remote_peers_exhausted.md>.

This is the recurring failure of this pipeline, not a one-off: nearly every time
the publish path has depended on `bb remote build` carrying bytes back across the
GitHub↔BuildBuddy boundary, it has broken. The fix that stuck for `bazel-ci`
(moving to `--script`) stuck because it deleted that download, not because it
moved work. Reading the event stream deletes it from the other side.

**Print the facts from the runner's stdout.** Works — `bb remote --script` can
compute digests on the runner and print them for the workflow to parse out of the
step log. But it invents a channel for data BuildBuddy already serves
structurally, and still costs a runner VM per planner.

**`bazel test <pattern> --experimental_remote_require_cached`.** Genuinely
useful: it succeeds only if every test in the pattern is already cached-passing
_for this commit's action keys_, so it proves freshness rather than provenance,
and it takes about a second. Verified to gate test actions, not just build ones.
Rejected here only because it costs a Bazel invocation — and therefore a runner
VM — to learn what `testSummary` in the stream already says for free. Worth
remembering if provenance ever stops being good enough.

**Push from the plan job, dropping the per-image fan-out.** Tempting once the
planner works: a merge typically needs zero or one image published, so `push`
allocates a runner mostly to discover it has nothing to do, and its ~45-60s of
checkout-and-devshell setup dwarfs the push. Rejected because the fan-out is not
there for the typical merge. Each push job builds its image's OCI layout and
downloads it before pushing, and that download is irreducible; one job doing it
for every changed image serialises them onto one runner's disk. The burst is real
and not hypothetical — 8 images in one run on 2026-08-27, after a string of
superseded runs left their changes outstanding, and a base-image bump can move the full image roster. `fail-fast: false` also keeps one image's failing test gate from taking the
other publishes down with it.

The two modules stay separate for the same reason and no other: `plan_image_pushes`
runs once, `push_image` runs per image, and what they share is already in
`image_registry`, leaving one comparison line apiece. Reconsider if the
`testSummary` gate below lands, which deletes the per-image `bb remote test` and
so most of what the fan-out buys.

**Shell out to `bbapi`.** `bbapi` reads the same stream and grew the same
capability for humans. The planners must not call it: `bbapi` is itself one of
the artifacts in `artifact_targets.json`, so a release planner depending on the
_released_ `bbapi` would be circular, and a stale pin in `nix/artifact-pins.json`
would fail the plan on an unrecognised flag. `bes.py` is stdlib-only for the same
reason the RBE worker image does not live in the registry it publishes to.

**Fetch the runner's own uploaded artifacts.** The hosted-runner action is
created with `DoNotCache: true`, and its blobs become unreachable within about a
second. That is the "Exhausted all peers" bug; the inner Bazel invocation's
outputs are ordinary cached actions and are not affected.

**Publish from inside the runner VM.** Deferred, not rejected — see
<../TODO.md>. Pushes stay on the GitHub runner, so no registry credential enters
a recycled Firecracker VM.

## What this leans on

- **BES retention.** If BuildBuddy ages a stream out before the planner reads
  it, the planner fails open and publishes everything. Unmeasured; publishing
  runs minutes after the build, so it has not bitten.
- **Provenance, not freshness.** The planner trusts that the invocation it was
  handed is this commit's build. The id comes from `bazel-ci` in the same run,
  which is as direct as it gets, but it is an assumption where
  `--remote_require_cached` would be a proof.
- **Stream size.** The planners read a large stream from both the test and build
  invocations. Re-measure if `//...` grows enough for the API request to become
  a material part of publish latency.
- **`bazel-ci` staying unstamped.** Stamping it, or moving `release-artifact`'s
  hash onto its stamped build, breaks the correspondence above. Neither
  mis-publishes — every tag would simply stop matching and every release would
  be republished once per commit — but that is this document's whole subject
  undone, and silently.
