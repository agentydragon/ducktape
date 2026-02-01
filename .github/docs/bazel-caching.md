# Bazel Caching in CI

## Overview

Bazel CI uses two complementary caching layers:

1. **BuildBuddy remote cache** — caches action results (build outputs, test results) across all runs. Configured via `setup-buildbuddy` action.
2. **GHA repository cache** — caches Bazel's `repository_cache` (compressed downloads of external deps) to avoid re-fetching on every run.

## Why repository_cache only?

Bazel's local state under `~/.cache/bazel` breaks down as:

| Directory                  | Size   | Purpose                           |
| -------------------------- | ------ | --------------------------------- |
| `output_base/external/`    | ~9.5GB | Extracted external repos          |
| `_bazel_*/cache/repos/v1/` | ~2GB   | Compressed downloads (repo cache) |
| `_bazel_*/install/`        | ~192MB | Extracted Bazel installation      |
| `~/.cache/bazelisk`        | ~62MB  | Bazelisk binary                   |

`output_base/external/` is dominated by the LLVM toolchain (~8.2GB extracted). This alone exceeds the 10GB GHA per-repo cache limit.

The `repository_cache` stores compressed downloads (~2GB). Restoring it lets Bazel skip network fetches during analysis. Extraction is local I/O — fast for everything except LLVM (which takes ~1-2 minutes).

BuildBuddy handles action-level caching (build outputs, test results), so the GHA cache only needs to cover the analysis-phase download cost.

## Cache key strategy

```
bazel-repo-cache-<hash of MODULE.bazel + MODULE.bazel.lock>
```

- **Shared across all CI jobs** — repository_cache contents are identical regardless of which job (build, test, lint, typecheck) populated them. No per-job slug.
- **`restore-keys: bazel-repo-cache-`** — on dependency changes, the previous cache is partially restored (most downloads unchanged).
- **Single cache entry** (~2GB) fits comfortably within the 10GB limit, leaving room for other caches (nix, pre-commit, ansible roles).

## Cache flow

```
compute-targets job
  ├── restore repository_cache (bazel-repo-cache action)
  ├── bazel-diff queries (partial repo fetching)
  ├── bazel fetch //... (prewarm — downloads all remaining repos)
  └── save repository_cache (bazel-repo-cache-save action)
      │
      ▼  (downstream jobs restore the same cache key)
  ┌───────────────────────────────────────────┐
  │ bazel-build / bazel-test / bazel-lint /   │
  │ bazel-typecheck / pre-commit / ...        │
  │   └── restore repository_cache            │
  │       (repos already downloaded, only     │
  │        extraction needed)                 │
  └───────────────────────────────────────────┘
```

The `compute-targets` job runs first and seeds the cache. Downstream jobs restore it and skip downloads entirely.

## Cached paths

- `~/.cache/bazelisk` — Bazelisk-downloaded Bazel binary
- `~/.cache/bazel/_bazel_runner/cache/repos/v1` — Bazel repository cache

The `_bazel_runner` segment assumes the GHA runner username is `runner` (standard on `ubuntu-latest`).

## Per-entry caching (not implemented)

`bazel-contrib/setup-bazel` supports per-external-repo caching of `output_base/external/` via its `external-cache` option. This would give finer-grained cache invalidation but doesn't help with the LLVM size problem (8.2GB extracted for one repo).

Per-entry caching of `repository_cache` (separate GHA cache key per SHA256 download) is theoretically possible but has no established precedent. The 2GB single-blob approach with `restore-keys` fallback provides good incremental behavior for the current repo size.

## Alternatives considered

| Approach                                   | Size   | Pros                 | Cons                    |
| ------------------------------------------ | ------ | -------------------- | ----------------------- |
| Cache full `~/.cache/bazel`                | ~12GB  | Fastest cold start   | Exceeds 10GB GHA limit  |
| Cache `output_base/external/` minus LLVM   | ~1.3GB | No extraction cost   | Fragile exclusion       |
| Cache `repository_cache` only              | ~2GB   | Simple, within limit | Extraction cost on miss |
| `bazel-contrib/setup-bazel` external-cache | Varies | Per-repo granularity | LLVM still 8.2GB        |
| No GHA cache, BuildBuddy only              | 0      | Simplest             | Slow analysis phase     |
