# `bb remote build` post-run "Exhausted all peers" — RCA + workaround

**Status:** active workaround in CI; upstream BuildBuddy bug not yet filed.

## Symptom

`bb remote build //...` (and any other `bb remote build` / `bb remote run`)
exits 1 immediately after the runner reports completion, with:

```
Remote run completed at 2026-05-03 00:46:52.004 UTC
download blob: rpc error: code = FailedPrecondition desc =
  read 66e1f2ff8901a52ba79a20ebfc3dc075a5bb8b36c61f93f8254e63393065ae00/24:
  rpc error: code = NotFound desc = Exhausted all peers attempting to read
  "66e1f2ff8901a52ba79a20ebfc3dc075a5bb8b36c61f93f8254e63393065ae00".
##[error]Process completed with exit code 1.
```

The Bazel build (and tests) **succeed** on the runner — `INFO: Build completed
successfully` appears before the failure. The crash is purely in `bb`'s
client-side post-run download.

First seen: **2026-04-30 ~10:56 UTC** on devel CI. Has been hitting almost every
Bazel CI run since 2026-05-02. Reproducible from any session, not just CI.

## Affected CI

The `Build + lint` step of `.github/workflows/bazel-ci.yml`. The `Test` step
runs `bb remote test //...` and is unaffected (see "Why" below). Sample
failures:

- Run [25266572770](https://github.com/agentydragon/ducktape/actions/runs/25266572770), job 74081984808, branch `claude/add-secret-banner-DiiPK`
- Run [25266455677](https://github.com/agentydragon/ducktape/actions/runs/25266455677), job 74081696418, branch `claude/debundle-strip-tana`
- Run [25265709835](https://github.com/agentydragon/ducktape/actions/runs/25265709835), job 74079772131, branch `claude/debundle-rust-use-statements`
- Run [25265862077](https://github.com/agentydragon/ducktape/actions/runs/25265862077), job 74080175153, branch `devel`

In every CI failure, the same digest is requested:
`66e1f2ff8901a52ba79a20ebfc3dc075a5bb8b36c61f93f8254e63393065ae00/24`.

## Why

`bb` source [`cli/remotebazel/remotebazel.go`](https://github.com/buildbuddy-io/buildbuddy/blob/master/cli/remotebazel/remotebazel.go):

```go
if bazelCmd == "build" || (bazelCmd == "run" && !*runRemotely) {
    fetchOutputs = true
}
...
if opts.FetchOutputs && exitCode == 0 {
    downloadOutputs(...)   // GetBlob(...) for each main output + runfiles
}
```

`downloadOutputs` resolves output digests from the inner Bazel invocation's
BES events (main outputs from `lookupBazelInvocationOutputs` →
`GetInvocation`; runfiles from `RunTargetAnalyzed` events). It then calls
`cachetools.GetBlob` for each — and that's where `download blob: …` originates.

`bb remote test` doesn't set `fetchOutputs`. `bb remote --script` skips this
path entirely. So the bug only appears when the bb CLI tries to pull build
outputs back to the local workspace.

### Why the blobs are missing

The runner action is created with `DoNotCache: true` hardcoded at
[`enterprise/server/hostedrunner/hostedrunner.go:278`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/hostedrunner/hostedrunner.go).
That is correct semantically — the wrapper action has unique inputs (invocation
ID, patches, timeout) and would never hit the action cache.

`DoNotCache` is supposed to disable **action result** caching only;
content-addressed **CAS blobs** uploaded by the action should persist
independently. But in current BuildBuddy production, every blob produced by a
`DoNotCache` runner action becomes unreachable within ~1s of the action
completing. Even the action's own `stderr` and `vm_log_tail.txt` blobs
(referenced from `bb execution get`) fail with the same `Exhausted all peers`
error. The cache-write path is uploading blobs that are not actually
retrievable from peers.

This is the upstream BuildBuddy bug. We don't have insight into PebbleCache /
distributed cache internals.

## Reproduction

Any session with `BUILDBUDDY_API_KEY` set:

```bash
# Fails:
bb remote build //devinfra:gazelle --config=rbe
# → download blob: ... Exhausted all peers ...

# Works (proves the bug is in downloadOutputs, not the runner):
bb remote --script="bazel build //devinfra:gazelle --config=rbe"

# Inspect the missing blob — same error:
bb download <hash>/<size>
bbapi cache metadata <hash> <size>
```

Demonstrating that even the runner's own stderr is unreachable:

```bash
bb remote --invocation_id_file=/tmp/inv.txt build //devinfra:gazelle --config=rbe
bbapi invocation $(cat /tmp/inv.txt)              # note Child: <id>
bbapi execution <inv-id> --json                   # find executeResponseDigest
# pull stderr digest from the execution response
bb download <stderr-hash>/<stderr-size>           # also "Exhausted all peers"
```

## Workaround

Wrap the build command in `--script`:

```bash
bb remote --script="bazel build --config=rbe //..."
```

`--script` keeps `bb` away from `downloadOutputs`. The build still happens
remotely and remote cache writes still occur — you just don't get the
build outputs auto-downloaded to the local `bb-out/` tree. For CI we don't
need the artifacts back on the GHA runner anyway (`push-images.yml` does its
own artifact download and is unaffected — see TODO below).

For local `bbr` use (`bbr build //target`), users who want artifacts back can
either (a) use `--remote_download_regex` and accept the bug for that
invocation, or (b) `bb remote --script "bazel build //target"` then re-run
`bazel build //target` locally — the second invocation hits the now-warm RBE
cache and materializes outputs via Bazel's normal download path, not via
`bb`'s broken `downloadOutputs`.

## CI fix

`.github/actions/bb-remote/action.yml` gained a `script` input.
`.github/workflows/bazel-ci.yml` collapsed its two-step `Test` + `Build + lint`
into a single `bb remote --script` invocation. Side benefit: 1 Firecracker VM
allocation per CI run instead of 2.

## Reference data (for upstream report)

- Local repro invocation: `bf3aafbf-fcab-4d72-9509-57d6b4abbb68` (workflow), child `6b93e3de-51f9-4a46-98ed-a27db464ab3f`
- Local repro execution: `bb-snapshot/uploads/79487951-4994-4eaa-9475-fc9df7adab10/blobs/blake3/4731968eedecefa557dc131e488ed915304ca474b9ed3fe5a16d0cccf5789a38/150`
- Action mnemonic: `RemoteBazelRun`
- Action metadata: `doNotCache: true`, `fileUploadCount: 4`, `fileUploadSizeBytes: 97472`
- Failing CAS digests (all return `Exhausted all peers`):
  - `71eb57bd65aa8516fcc15da1aee0cb71ca9993c92a2796b0b516c2c8f2ee2fe2/3813` (the blob `bb` wants)
  - `8860153709b0953142f8d6f18b600cc0df32dcd8a49a966731c435384f01d090/88491` (action stderr)
  - `a3ce63f5a76f392cc8e393fd5483ae4f700e2d7ae4107446d19a4e2c7bc30caa/8981` (vm_log_tail.txt)
  - `af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262/0` (action stdout — empty file digest)

## TODO

- [ ] File upstream issue at <https://github.com/buildbuddy-io/buildbuddy/issues/new>. Draft body below.
- [x] Audit other `bb remote build` consumers. `.github/workflows/push-images.yml` no longer uses `--remote_download_regex`; its four `bb-remote` steps use `args: build … --remote_download_outputs=toplevel` and publish images successfully, so they are not blocked by this bug.
- [ ] When upstream is fixed, revert the `--script` workaround in `bazel-ci.yml` and remove the `script` input from `bb-remote/action.yml`.

### Draft upstream issue

> **Title:** `bb remote build` post-run `downloadOutputs` fails with `Exhausted all peers` for blobs the runner just uploaded
>
> **Symptom**
>
> Every `bb remote build //target` invocation against `remote.buildbuddy.io`
> fails after the runner reports completion:
>
> ```
> Remote run completed at <ts>
> download blob: rpc error: code = FailedPrecondition desc = read <hash>/<size>:
>   rpc error: code = NotFound desc = Exhausted all peers attempting to read "<hash>".
> ```
>
> The inner Bazel build itself succeeds — `INFO: Build completed successfully`
> appears before the failure. `bb remote --script="bazel build …"` works
> (skips `downloadOutputs`). `bb remote test //…` works (no `fetchOutputs`).
>
> **Repro**
>
> ```
> bb remote build //some/target --config=rbe   # fails
> bb remote --script="bazel build //some/target --config=rbe"   # succeeds
> ```
>
> **Investigation**
>
> The runner action is created with `DoNotCache: true`
> ([`hostedrunner.go:278`](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/hostedrunner/hostedrunner.go)).
> Its execution metadata reports `fileUploadCount: 4`,
> `fileUploadSizeBytes: 97472`, so output blobs went into CAS. But within ~1s
> of completion, **every** referenced blob is unreachable — including action
> stderr, `vm_log_tail.txt`, and the empty-file digest:
>
> ```
> bb download 8860153709b0953142f8d6f18b600cc0df32dcd8a49a966731c435384f01d090/88491
> # → Exhausted all peers
> bbapi cache metadata 8860153709b0953142f8d6f18b600cc0df32dcd8a49a966731c435384f01d090 88491
> # → Exhausted all peers
> ```
>
> Hypothesis: distributed-cache (PebbleCache) writes for `DoNotCache: true`
> action outputs are not making it to the peer set, even though the
> `BatchUpdateBlobs` / `Write` RPCs return success during the action.
>
> **Reference invocations / digests** (in our org, available to BB support):
>
> - Workflow invocation: `bf3aafbf-fcab-4d72-9509-57d6b4abbb68`, child `6b93e3de-51f9-4a46-98ed-a27db464ab3f`
> - Execution: `bb-snapshot/uploads/79487951-4994-4eaa-9475-fc9df7adab10/blobs/blake3/4731968eedecefa557dc131e488ed915304ca474b9ed3fe5a16d0cccf5789a38/150`
> - Missing CAS digests: `71eb57bd…/3813`, `8860153709…/88491`, `a3ce63f5…/8981`, `af1349b9…/0`
>
> First seen ~2026-04-30, persists. Affects all `bb remote build` users in our
> org; not specific to a particular target, container image, or
> `runner_exec_properties` configuration.
