# Cold-Start Query Profile Analysis (2026-04-01)

Bazel 8.6.0, ext4 filesystem, gVisor sandbox.
Query: `kind(".*_test", //...)` from a fully shut-down server.
Wall clock: **15.4s** (429 targets found).

## Critical path

```
 0.0s ─────── Launch Blaze (JVM startup) ──────── 1.4s
 1.4s ─ gap ─ 1.9s
 1.9s ─────── Module resolution (BCR fetches) ─── 5.6s
 5.6s ─────── queryEnv.evaluateQuery ──────────── 14.9s
                ├─ Module extension eval (pip, npm, go, python)
                ├─ Repository fetching (3042 repos, parallelized)
                └─ Package loading (655 packages, parallelized)
```

| Phase                     | Start | End   | Wall      | Notes                         |
| ------------------------- | ----- | ----- | --------- | ----------------------------- |
| JVM startup               | 0.0s  | 1.4s  | 1.4s      | `Launch Blaze`                |
| Server init + module file | 1.4s  | 1.9s  | 0.5s      | Setup, `handleDiffs`          |
| Module resolution (BCR)   | 1.9s  | 5.6s  | 3.6s      | Fetch MODULE.bazel from BCR   |
| Query evaluation          | 5.6s  | 14.9s | 9.4s      | Extensions + packages + repos |
| **Total**                 |       |       | **15.0s** |                               |

### Why only 15s instead of 29s?

The previous benchmark showed ~29s for the first cold query because that was
the very first query on a fresh `--output_base` (no on-disk repo cache).
This run reuses a warm on-disk cache from earlier bench runs — Bazel doesn't
need to re-download module source archives, only re-fetch MODULE.bazel files
from BCR and re-evaluate module extensions.

## Phase 1: JVM startup (1.4s)

The Bazel client launches the server JVM. The 1.4s here vs 11.6s in the
first-ever cold start is because the JVM class data sharing (CDS) archive
and installation are already warm on disk.

## Phase 2: Module resolution from BCR (3.6s)

Bazel's first Skyframe evaluation fetches MODULE.bazel files from the
Bazel Central Registry (BCR) to resolve the module dependency graph:

| Module                   | Fetch time |
| ------------------------ | ---------- |
| `rules_rust@0.69.0`      | 0.52s      |
| `bazel_skylib@1.9.0`     | 0.52s      |
| `platforms@1.0.0`        | 0.42s      |
| `rules_python@1.9.0`     | 0.11s      |
| `bazel_features@1.10.0`  | 0.18s      |
| `bazel_lib@3.0.0-beta.1` | 0.08s      |
| (many more, overlapping) | ...        |

These are **network I/O bound** — sequential HTTP GETs to
`bcr.bazel.build` through the auth proxy. Many are parallelized by
Skyframe but the dependency chain between modules creates serialization.

## Phase 3: Query evaluation (9.4s)

The second Skyframe evaluation runs the actual query. This evaluates
module extensions, fetches external repositories, loads BUILD files,
and evaluates the `kind(".*_test", ...)` filter.

### Module extension evaluation

| Extension                | Wall time | Dominant function          |
| ------------------------ | --------- | -------------------------- |
| `pip.bzl%pip`            | 5.12s     | `parse_requirements_txt`   |
| `npm/extensions.bzl%npm` | 2.86s     | `_npm_lock_imports_bzlmod` |
| `go_sdk`                 | 1.17s     | `fetch_sdks_by_version`    |
| `python.bzl%python`      | ~0.6s     | `_get_toolchain_config`    |
| `go_deps`                | ~0.6s     | `sums_from_go_mod`         |

These are **CPU-bound** Starlark execution — parsing lockfiles
(requirements_bazel.txt, pnpm-lock.yaml, go.sum) and generating
repository rule declarations for each dependency.

### Repository fetching (parallelized)

| Category  | Count | Aggregated time | Avg per repo |
| --------- | ----- | --------------- | ------------ |
| npm links | 2937  | 125.7s          | 0.043s       |
| pip/pypi  | 6     | 0.3s            | 0.05s        |
| other     | 101   | 4.9s            | 0.049s       |
| **Total** | 3042  | 130.8s          | 0.043s       |

The 130.8s of aggregated time runs in parallel across Skyframe threads,
compressing into the ~9.4s wall time. Each npm link repo creates a
symlink-tree workspace directory — this is **I/O-bound** (filesystem
operations on ext4).

### Package loading (parallelized)

| Stat            | Value                                            |
| --------------- | ------------------------------------------------ |
| Packages loaded | 655                                              |
| Aggregated time | 60.3s                                            |
| Slowest package | 0.44s (`props/specimens/ducktape/2025-11-26-00`) |

Package loading runs in parallel with repo fetching. Each package
parses its BUILD file and evaluates macros (Starlark). The `specimens`
packages are slow due to large `glob()` patterns matching many files.

## Resource utilization

| Resource | Usage                             | Evidence                                 |
| -------- | --------------------------------- | ---------------------------------------- |
| CPU      | High during Starlark eval         | 71.5s aggregated Starlark function calls |
| Disk I/O | High during repo fetch            | 3042 repos creating symlink trees        |
| Network  | Moderate during BCR fetch         | ~20 MODULE.bazel fetches, 0.05–0.5s each |
| Memory   | 26 GC notifications (0.42s total) | Minor GC pressure                        |

## Comparison across scenarios

| Scenario         | Wall time | JVM   | BCR  | Extensions | Repos+Pkgs |
| ---------------- | --------- | ----- | ---- | ---------- | ---------- |
| First-ever cold  | ~29s      | 11.6s | ~4s  | ~7s        | parallel   |
| Subsequent cold  | 15.4s     | 1.4s  | 3.6s | ~5s        | parallel   |
| First warm query | 6.1s      | —     | —    | 5.9s       | parallel   |
| Subsequent warm  | 0.3s      | —     | —    | cached     | cached     |

The dominant costs shift as caches warm up:

- **First-ever cold**: JVM startup dominates (no CDS archive)
- **Subsequent cold**: BCR fetches + extension eval dominate
- **First warm**: Extension eval dominates (no Skyframe cache)
- **Subsequent warm**: Filesystem diff scanning dominates (0.25s)
