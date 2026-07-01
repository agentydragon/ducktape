# Bazel Caching in CI

## Overview

Bazel CI uses two caching layers:

1. **BuildBuddy remote cache** — caches action results (build outputs, test results) across all runs. 98%+ hit rate in practice. Configured via `setup-bazel` action.
2. **GHA repository cache** — caches Bazel's `repository_cache` (compressed downloads of external deps). Uses the unified `actions/cache@v4` action for restore+save.

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

```text
bazel-ci job (.github/workflows/bazel-ci.yml)
  ├── restore repository_cache (setup-bazel action)
  ├── bb-remote --script:
  │     ├── (PR only) target-determinator computes affected target set
  │     └── bazel test + build (`//...` on devel, affected-only on PRs)
  └── post step: save repository_cache (automatic, only on exact-key miss)
```

The `setup-bazel` action uses the unified `actions/cache@v4`, which saves the cache as a post step on job success. The unified action only saves when the exact key was NOT found during restore, avoiding duplicate entries.

There is **no prewarm step**. The single `bazel-ci` job populates the repository_cache during its own analysis phase; there are no downstream Bazel jobs sharing that cache within a single CI run.

On PRs, `bazel-ci` first invokes `target-determinator` (packaged in `.#rbetools`) to compute the set of targets affected by the diff vs. `origin/devel`, and passes that set to `bazel test`/`bazel build` via `--target_pattern_file`. Devel-branch push runs test/build `//...`. See `.github/workflows/bazel-ci.yml` for the current script.

## Duplicate-key problem (historical)

The former `bazel-repo-cache-save` action used `actions/cache/save@v4`, which creates a new cache entry even when the same key already exists. Multiple CI runs created conflicting entries per key. The GHA cache service responded with HTTP 400 on all restore attempts, making the cache useless across all jobs.

The fix was to switch to the unified `actions/cache@v4` action, which only saves when the exact key was not found during restore. This prevents duplicate entries by design.

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
