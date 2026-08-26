# How publishing decides what to publish

A merge to `devel` can publish 42 container images and 50 release artifacts.
Almost none of them changed. This is how CI works out which ones did.

## The rule

**Build once, then read what that build produced.** `bazel-ci` already runs
`bazel test //...` and `bazel build //...` on every devel push, so by the time
anything is published, every artifact has been built and every test has a
verdict. Nothing downstream rebuilds any of it.

BuildBuddy keeps that invocation's Build Event Protocol stream and serves it as
JSON. One request — 33 MB, about two seconds for a full sweep — carries three
things at once:

- every output file, with its content digest, path prefix and producing label;
- a `testSummary` verdict per test target;
- `bytestream://` URIs for fetching any of those files.

`devinfra/ci/bes.py` reads it. `plan_releases.py` and `plan_image_pushes.py`
consume it. `bazel-ci` publishes its invocation ids as a workflow output and
`ci.yml` passes them down.

## Identity

A release's tag is `<pkg>-<first 12 of its content hash>`. For a single-asset
release that hash **is** the artifact's sha256, which the stream already
reports — so no bytes are fetched at all. Multi-asset releases (only `aiquota`)
compose the same hash from the same digests via
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

## Fail open, always

`release` and `push-images` run under `always() && !cancelled()`, so they run
even when `bazel-ci` failed or was skipped. **Anything short of proof that an
item is unchanged republishes it.** No invocation, an output the stream never
mentions, an unreadable blob, an unreachable registry — all keep the item in the
matrix, where its own job checks properly and fails loudly.

The asymmetry is deliberate: wrongly including an item costs one job, wrongly
excluding one silently skips a deployment. A planner that cannot prove anything
must degrade to the full fan-out it replaced, never to publishing nothing.

## What a `//...` sweep cannot cover

Some rows always take the slow path, for reasons that are not going to change:

| Row                   | Why                                                                                |
| --------------------- | ---------------------------------------------------------------------------------- |
| `manifold-mcp-server` | `@ducktape_manifold_mcp_server//:image` — external repo; `//...` does not reach it |
| `aw-importer`         | `@ducktape_activitywatch//importer:…` — same                                       |
| `gterm-theme`         | `tags = ["manual"]` (needs libgirepository, libdbus), so wildcards skip it         |
| `debundle`            | builds under `-c opt`, a different configuration                                   |

Measured coverage on devel's sweep: **41 of 42** images, **47 of 50** releases.

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
- **Stream size.** 33 MB per read, twice (bazel-ci reports a test and a build
  invocation). Fine at this repo's size; worth re-measuring if `//...` grows
  much larger.
- **`bazel-ci` staying unstamped.** Stamping it, or moving `release-artifact`'s
  hash onto its stamped build, breaks the correspondence above. Neither
  mis-publishes — every tag would simply stop matching and every release would
  be republished once per commit — but that is this document's whole subject
  undone, and silently.
