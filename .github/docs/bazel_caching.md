# Bazel Caching in CI

## Overview

Bazel CI uses two caching layers:

1. **BuildBuddy remote cache** — caches action results (build outputs, test results) across all runs. 98%+ hit rate in practice. Configured via `setup-buildbuddy` action.
2. **GHA repository cache** — caches Bazel's `repository_cache` (compressed downloads of external deps). Restore-only in the current configuration; see below.

## Why repository_cache only?

Bazel's local state under `~/.cache/bazel` breaks down as:

| Directory                  | Size   | Purpose                           |
| -------------------------- | ------ | --------------------------------- |
| `output_base/external/`    | ~9.5GB | Extracted external repos          |
| `_bazel_*/cache/repos/v1/` | ~2GB   | Compressed downloads (repo cache) |
| `_bazel_*/install/`        | ~192MB | Extracted Bazel installation      |
| `~/.cache/bazelisk`        | ~62MB  | Bazelisk binary                   |

`output_base/external/` is dominated by the LLVM toolchain (~8.2GB extracted), which alone exceeds the 10GB GHA per-repo cache limit.

The `repository_cache` stores compressed downloads (~2GB). Restoring it lets Bazel skip network fetches during analysis. BuildBuddy handles action-level caching (build outputs, test results), so the GHA cache only needs to cover the analysis-phase download cost.

## Cache key strategy

```
bazel-repo-cache-<hash of MODULE.bazel + MODULE.bazel.lock>
```

- **Shared across all CI jobs** — repository_cache contents are identical regardless of which job populated them.
- **`restore-keys: bazel-repo-cache-`** — on dependency changes, the previous cache is partially restored.
- **Single cache entry** (~2GB) fits within the 10GB limit.

## Cache flow (current)

```
compute-targets job
  ├── restore repository_cache (bazel-repo-cache action, restore-only)
  ├── bazel-diff queries
  └── upload targets artifact

  downstream jobs (bazel-check, bazel-test, ...)
    └── restore repository_cache (setup-bazel → bazel-repo-cache)
        (BuildBuddy serves all action outputs; analysis-phase repo fetching
         is handled locally or via BuildBuddy's CAS)
```

There is **no prewarm step**. The prewarm (`bazelisk fetch //...`) was removed because:

1. It added 250–280s to the `compute-targets` critical path on every run.
2. The GHA repo cache was broken by duplicate-key entries (see below), so the prewarm had zero payoff.
3. Downstream jobs complete in 7–8 min regardless of repo cache status — BuildBuddy handles everything.
4. Profiling showed `compute-targets` at ~5 min with prewarm vs ~1 min without.

## Duplicate-key problem (historical)

The former `bazel-repo-cache-save` action used `actions/cache/save@v4`, which creates a new cache entry even when the same key already exists. Multiple CI runs created conflicting entries per key. The GHA cache service responded with HTTP 400 on all restore attempts, making the cache useless across all jobs (including jobs that never wrote to it).

After removing the prewarm and cache save from `compute-targets`, this death spiral stops. Duplicate entries with key `bazel-repo-cache-*` from before this fix should be manually deleted via the [GHA cache UI](https://github.com/agentydragon/ducktape/actions/caches) or:

```bash
# List duplicates
gh api '/repos/agentydragon/ducktape/actions/caches?per_page=100' \
  --jq '.actions_caches[] | select(.key | startswith("bazel-repo-cache")) | "\(.id) \(.key) \(.size_in_bytes)"'

# Delete by ID (requires repo admin rights)
gh api --method DELETE /repos/agentydragon/ducktape/actions/caches/<id>
```

Known wrong entries as of 2026-02-18 (127MB, bazelisk-only, no repo archives):

- `2898171970` — `bazel-repo-cache-1d47f7dbeb8d2342`
- `2884068105` — `bazel-repo-cache-a6e9d5d5091049df`
- `2881681652` — `bazel-repo-cache-74162b57e0b543c5`
- `2876727684` — `bazel-repo-cache-0598ae10e1e3508f`

## Cached paths

- `~/.cache/bazelisk` — Bazelisk-downloaded Bazel binary
- `~/.cache/bazel/_bazel_runner/cache/repos/v1` — Bazel repository cache

The `_bazel_runner` segment assumes the GHA runner username is `runner` (standard on `ubuntu-latest`).

## Alternatives considered

| Approach                                   | Size   | Pros                 | Cons                               |
| ------------------------------------------ | ------ | -------------------- | ---------------------------------- |
| Cache full `~/.cache/bazel`                | ~12GB  | Fastest cold start   | Exceeds 10GB GHA limit             |
| Cache `output_base/external/` minus LLVM   | ~1.3GB | No extraction cost   | Fragile exclusion                  |
| Cache `repository_cache` only              | ~2GB   | Simple, within limit | Extraction cost on miss            |
| `bazel-contrib/setup-bazel` external-cache | Varies | Per-repo granularity | LLVM still 8.2GB                   |
| No GHA cache, BuildBuddy only              | 0      | Simplest, proven     | Repo fetching happens per-job      |
| Prewarm in compute-targets + save          | ~2GB   | Seeds cache once     | +4.5 min critical path, was broken |
