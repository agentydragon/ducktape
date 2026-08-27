# Bazel Caching in CI

## Overview

Bazel CI uses two caching layers:

1. **BuildBuddy remote cache** — caches action results (build outputs, test results) across runs. Hosted builds use `.github/actions/bb-remote`; workflows that run Bazel directly configure the same remote cache after `.github/actions/setup-bazel`.
2. **GHA repository cache** — caches Bazel's `repository_cache` (compressed downloads of external dependencies) for workflows that run Bazel directly on GitHub-hosted runners. It uses unified `actions/cache@v6` restore and save behavior.

## Why repository_cache only?

Bazel's local state under `~/.cache/bazel` breaks down as:

| Directory                  | Size   | Purpose                           |
| -------------------------- | ------ | --------------------------------- |
| `output_base/external/`    | ~9.5GB | Extracted external repos          |
| `_bazel_*/cache/repos/v1/` | ~2GB   | Compressed downloads (repo cache) |
| `_bazel_*/install/`        | ~192MB | Extracted Bazel installation      |
| `~/.cache/bazelisk`        | ~62MB  | Bazelisk binary                   |

`output_base/external/` is dominated by the LLVM toolchain (~8.2GB extracted), which alone exceeds the 10GB GHA per-repo cache limit.

The `repository_cache` stores compressed downloads (~2GB). It is content-addressable: each archive is stored by its content hash, so restoring it lets Bazel skip network fetches during analysis even when only some dependencies changed. BuildBuddy handles action-level caching (build outputs, test results), so the GHA cache only needs to cover the analysis-phase download cost.

## Cache key strategy

```
bazel-repo-cache-<hash of MODULE.bazel + MODULE.bazel.lock>
```

- **Shared across all CI jobs** — repository_cache contents are identical regardless of which job populated them.
- **`restore-keys: bazel-repo-cache-`** — on dependency changes, the previous cache is partially restored (content-addressable, so unchanged downloads are reused).
- **Single cache entry** (~2GB) fits within the 10GB limit.

## Cache flow

Workflows that run Bazel directly on a GitHub Actions runner call
`.github/actions/setup-bazel`. Each job restores the exact cache key, falling
back to the newest `bazel-repo-cache-` entry after dependency changes. Bazel
and Bazelisk populate the restored directories as the job runs.

The action uses unified `actions/cache@v6`, which saves the populated cache as
a post step only when the exact key was absent during restore. There is no
dedicated prewarm or `compute-targets` job. BuildBuddy-hosted `bb remote` runs
execute in separate runner VMs and use BuildBuddy's cache rather than this
GitHub-runner filesystem cache.

## Undeclared test outputs under BwoB

`--remote_download_minimal` (Build without the Bytes) singles out test runner
actions. `RemoteOutputChecker.addTargetUnderTest` adds a test's outputs to the
download set only when `outputsMode != MINIMAL`, so under minimal the
`test.outputs/` tree stays remote-only.

That is invisible until a cache hit. BEP's per-test file list comes from
`TestRunnerAction.getTestOutputsMapping`, which stats each path on the local
filesystem, and Bazel does not populate the output filesystem on an action-cache
hit — an upstream limitation with a standing TODO in that method. A test served
from the local action cache therefore reports `test.log` and nothing else, while
the same test served from the remote cache reports its full `test.outputs/`,
because that path materializes the outputs. `devinfra/pr_visuals` consumes
exactly those artifacts, so a visual test that hit the local cache published
nothing and the run looked like it had no visual changes.

`test:rbe --remote_download_regex=bazel-out/.*/testlogs/.*/test.outputs(/.*)?`
puts the tree back in the download set. That fixes the cache-hit path too, not
just execution: `RemoteOutputChecker.shouldTrustMetadata` refuses to trust a
remote-only output that should have been downloaded, so the action cache cannot
hit while `test.outputs/` is remote-only. The cost lands only on tests that
wrote undeclared outputs — an empty tree has no children to distrust — and it is
a remote-cache round trip, not a re-execution.

The pattern spans the whole path because `RegexPatternOption` matches the entire
string, and its `.` is unescaped because a bazelrc consumes the backslash —
`--announce_rc` shows what Bazel actually received.
`shouldDownloadOutput` checks the child path and the tree root, so a pattern
matching either is enough.

### Rejected: --remote_download_toplevel

The blunt form of the same fix, and it does work — but `toplevel` downloads
every top-level target's important artifacts, which on `bazel test //...` means
every OCI image tarball lands on the runner. The regex asks for the few hundred
kilobytes of PNGs that BEP actually needs.

## Duplicate-key problem (historical)

The former `bazel-repo-cache-save` action used `actions/cache/save@v4`, which creates a new cache entry even when the same key already exists. Multiple CI runs created conflicting entries per key. The GHA cache service responded with HTTP 400 on all restore attempts, making the cache useless across all jobs.

The fix was to switch to the unified `actions/cache` action (currently v6), which only saves when the exact key was not found during restore. This prevents duplicate entries by design.

## Cached paths

- `~/.cache/bazelisk` — Bazelisk-downloaded Bazel binary
- `~/.cache/bazel/_bazel_runner/cache/repos/v1` — Bazel repository cache

The `_bazel_runner` segment assumes the GHA runner username is `runner` (standard on `ubuntu-latest`).

## Alternatives considered

| Approach                                   | Size   | Pros                 | Cons                         |
| ------------------------------------------ | ------ | -------------------- | ---------------------------- |
| Cache full `~/.cache/bazel`                | ~12GB  | Fastest cold start   | Exceeds 10GB GHA limit       |
| Cache `output_base/external/` minus LLVM   | ~1.3GB | No extraction cost   | Fragile exclusion            |
| Cache `repository_cache` only (current)    | ~2GB   | Simple, within limit | Extraction cost on miss      |
| `bazel-contrib/setup-bazel` external-cache | Varies | Per-repo granularity | LLVM still 8.2GB             |
| No GHA cache, BuildBuddy only              | 0      | Simplest             | ~5 min repo fetching per-job |
| Dedicated prewarm job + save               | ~2GB   | Seeds cache once     | Adds to the critical path    |
