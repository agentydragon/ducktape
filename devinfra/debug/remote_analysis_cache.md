# Bazel `--experimental_remote_analysis_cache` Investigation

**Date**: 2026-03-24
**Bazel version**: 8.6.0 (bazelisk 1.25.0)
**Remote cache**: BuildBuddy (grpcs://remote.buildbuddy.io)
**Environment**: Claude Code web (4 CPU, 16 GB RAM, 9p root filesystem, TLS-inspecting egress proxy)

## Summary

`--experimental_remote_analysis_cache` is an undocumented flag in Bazel 8.6.0 that accepts a
gRPC endpoint string (e.g., `grpcs://remote.buildbuddy.io`). It appears to be the open-source
surface of Google's internal **Skycache** project, which serializes Bazel's in-memory Skyframe
graph and stores it in a remote cache to accelerate cold starts.

**Status: The flag is accepted but its practical effect is unclear.** In our testing, the
flag did not produce measurable improvements. The bottleneck on this environment is
package loading through the proxied network and 9p filesystem, not analysis computation.
Further testing on native hardware with fast I/O is needed.

## Flag Details

- **Name**: `--experimental_remote_analysis_cache`
- **Type**: String (NOT boolean). Takes a gRPC endpoint URI.
- **Correct usage**: `--experimental_remote_analysis_cache=grpcs://remote.buildbuddy.io`
- **Wrong name**: `--experimental_remote_analysis_caching` (produces "did you mean?" error)
- **Not in `bazel help`**: The flag is accepted by the parser but not listed in `bazel help build`.
- **Silently accepts invalid values**: `--experimental_remote_analysis_cache=true` or
  `--experimental_remote_analysis_cache=foo` are accepted without error.

## Background: Skycache (BazelCon 2025)

At BazelCon 2025, Google's Shahan Yang presented Skycache — a system that serializes
Bazel's Skyframe graph and stores it in a remote key-value cache. Key points:

- **Purpose**: Eliminate the cold-start penalty where the loading/analysis phase must
  rebuild the entire Skyframe graph after server restart.
- **Top-down pruning**: On cache hit, entire subtrees of the graph are skipped.
- **Invalidation**: Compares local file state against cached baseline to find changes.
- **Serialization optimizations**: Nested sets (10x blowup fixed) and shared node internals.
- **Results**: Google saw some builds drop from 46s to 13s internally.
- **Open source status**: No public implementation yet; serialization code exists in Bazel,
  but integration with a KV store is needed.

BuildBuddy takes a different approach: Firecracker MicroVM snapshots that capture the
entire JVM state (including analysis cache) and restore it in milliseconds.

## Test Results

### Test Environment Limitations

The Claude Code web environment introduces significant overhead:

1. **9p root filesystem**: All file I/O goes through 9p protocol, ~10-100x slower than native
2. **TLS-inspecting egress proxy**: All network requests are proxied with JWT auth
3. **Auth proxy daemon timeout**: The hook daemon (which runs the local auth proxy) has a
   330s idle timeout. Long builds (>5 min) cause the proxy to die mid-build, failing
   remote cache and BES uploads.
4. **4 CPUs / 16 GB RAM**: Limited parallelism for Bazel's analysis phase.

These limitations mean loading/analysis of `//...` takes 6-10 minutes (dominated by
network and filesystem I/O), making it hard to isolate the analysis computation speedup
that `--experimental_remote_analysis_cache` targets.

### Benchmark: Single Target (`//devinfra/claude/auth_proxy:proxy`)

| Scenario | Elapsed | Notes |
|---|---|---|
| First build (cold server, warm repo cache) | 423.5s | Invocation `7c0b00b4` |
| After shutdown + with analysis cache | 39.7s | Invocation `6c5733b5` |
| After shutdown + without analysis cache | 39.9s | Invocation `aea333f9` |
| Warm server (no-op) | 0.7s | Invocation `c553789b` |
| After `--discard_analysis_cache`, no remote | 0.6s | Invocation `c2ed2599` |
| After `--discard_analysis_cache`, with remote | 0.5s | Invocation `5ad6cd60` |

**Observations**:
- After `bazel shutdown`, both with and without `--experimental_remote_analysis_cache`
  took ~40s. The local disk cache (repo cache in tmpfs output_base) dominates — analysis
  re-computation is not the bottleneck.
- After `--discard_analysis_cache`, re-analysis is fast (~0.5s for a single target)
  because packages are still loaded in the server.
- No measurable difference between with/without the remote analysis cache flag.

### Benchmark: Full Build (`//...` excluding terraform)

| Scenario | Elapsed | Processes | Notes |
|---|---|---|---|
| After `bazel clean` (baseline) | 356.5s | 5556 (46 remote cache hit) | Invocation `94ac8b83` |
| Warm server + analysis cache flag | 381.2s | 8722 (4387 remote cache hit) | BES failed |
| After shutdown + analysis cache flag | 545.5s | 5274 (24564 action cache hit) | Invocation `8aa5a345` |
| After expunge + analysis cache flag | >589s | 13 (failed) | Proxy died mid-build |

**Observations**:
- The analysis cache flag added overhead on warm builds (+25s, from 356→381s) — likely
  from serializing and uploading the Skyframe graph.
- After `bazel shutdown`, the build with analysis cache was SLOWER (545s vs 356s baseline).
  However, this is confounded by the proxy daemon dying during long builds.
- After `bazel clean --expunge`, builds consistently failed because the auth proxy daemon
  timed out during the 8+ minute loading phase.

### All BuildBuddy Invocation IDs

| Invocation ID | Scenario |
|---|---|
| `94ac8b83-5c4e-4c50-af90-1186b0da01d6` | Baseline `//...` after `bazel clean` |
| `8aa5a345-b14b-4117-8bf4-c7c050caa27e` | `//...` cold restart + analysis cache |
| `04f5f082-faa1-4a29-b8c2-2534499ed08d` | Focused targets after expunge (failed) |
| `7c0b00b4-ed49-4d6e-8ab8-b2ea862e5522` | Single target, first build with analysis cache |
| `6c5733b5-23a4-42d3-b9c0-70503b7bf420` | Single target, cold restart WITH analysis cache |
| `aea333f9-bc09-4087-8996-8e3ccc2a20d2` | Single target, cold restart WITHOUT analysis cache |
| `c553789b-f139-475b-bc96-dfe2c1dab5ba` | Warm server baseline |
| `cfd67305-34d5-414f-be64-5a30ffb47922` | Discard analysis cache (first) |
| `5db07589-ddbf-45f9-a108-fb404ff36992` | Discard analysis cache (second, re-analyze) |
| `680da004-d904-4d47-a1c8-a04aa66472a0` | Discard + remote analysis cache (upload) |
| `e1744e39-a3b0-445d-a585-9da9b9f50878` | Discard + remote analysis cache (restore) |
| `dc67f9b3-b01e-49cb-ba77-160e5631d1ed` | Single target warm + discard |
| `c2ed2599-8f23-4c24-9a17-147f08e912af` | Single target no remote analysis |
| `0d31fa80-003f-481a-8f53-4ae2f27c160b` | Single target with remote analysis (upload) |
| `5ad6cd60-ac81-40d9-99a3-1731fa9b303a` | Single target with remote analysis (restore) |
| `701cea36-9059-49ea-b73d-8f17dec74f00` | Flag acceptance test |

## Conclusions

### What We Know

1. **The flag exists in Bazel 8.6.0** and is accepted with a gRPC endpoint string.
2. **It's likely the open-source Skycache surface** from Google's BazelCon 2025 talk.
3. **It does not appear in `bazel help`**, suggesting it's highly experimental.
4. **No public documentation exists** — web searches return no results for this specific flag.
5. **BuildBuddy appears to support it** — no server-side errors when using their endpoint.

### What We Don't Know

1. **Whether it's actually functional** — the flag may be a stub or partially implemented.
   It silently accepts invalid endpoint strings without error.
2. **The actual speedup on native hardware** — our 9p/proxy environment is too slow to
   isolate the analysis cache benefit from I/O overhead.
3. **Whether BuildBuddy's server has specific support** — or if it just treats the cache
   entries as opaque blobs in the standard remote cache.
4. **Compatibility with `--remote_download_minimal`** — untested due to proxy issues.

### Recommendation

**Don't enable yet.** The flag is undocumented and its behavior is uncertain. Wait for:

1. Official Bazel documentation or release notes mentioning this flag.
2. BuildBuddy blog post or docs confirming server-side support.
3. Community reports of successful usage.

**Next steps to validate**:
- Test on native hardware (NixOS workstation) with direct internet — eliminates 9p and proxy overhead.
- Compare `bazel clean --expunge` builds with and without the flag on native hardware.
- Check BuildBuddy invocation details for analysis cache upload/download metrics.
- Monitor Bazel GitHub for Skycache-related PRs and issues.

## References

- [BazelCon 2025 recap (Julio Merino)](https://blogsystem5.substack.com/p/bazelcon-2025-recap) — Skycache overview
- [BazelCon 2025 recap (JetBrains CLion)](https://blog.jetbrains.com/clion/2025/11/bazelcon-2025/) — analysis caching discussion
- [The Many Caches of Bazel (EngFlow)](https://blog.engflow.com/2024/05/13/the-many-caches-of-bazel/) — Skyframe cache architecture
- [Skyframe invalidation with disk or remote cache (GitHub #22367)](https://github.com/bazelbuild/bazel/issues/22367) — related discussion
- [BuildBuddy remote caching explained](https://www.buildbuddy.io/blog/bazels-remote-caching-and-remote-execution-explained/)
