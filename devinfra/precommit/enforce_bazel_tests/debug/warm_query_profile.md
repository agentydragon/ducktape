# Warm Query Profile Analysis (2026-04-01)

Bazel 8.6.0, ext4 filesystem, gVisor sandbox, BuildBuddy RBE.
Target: `//util/bazel:workspace.py`.

## Summary

A warm-server `kind(".*_test", //...)` query takes **0.3s** — but only after
the Bazel server has fully loaded all module extensions and packages. The
**first** warm query after a cold start pays a one-time ~6s penalty for
module extension evaluation (pip, npm, go_sdk, etc.).

| Query (warm server)         | Wall time | Bottleneck                    |
| --------------------------- | --------- | ----------------------------- |
| `kind("py_test", //...)` #1 | 6.07s     | Module extension eval (5.9s)  |
| `kind(".*_test", //...)` #2 | 0.35s     | Filesystem diff check (0.26s) |
| `tests(//...)` #3           | 0.29s     | Filesystem diff check (0.22s) |

## Phase breakdown: first warm query (6.07s)

The first query after server startup must evaluate Bzlmod module extensions.
These are cached in Skyframe after the first evaluation — subsequent queries
skip them entirely.

| Phase                           | Wall time | Notes                     |
| ------------------------------- | --------- | ------------------------- |
| Module extension: `pip.bzl%pip` | 3.42s     | Parses requirements lock  |
| Module extension: `npm/npm`     | 2.20s     | Parses pnpm lockfile      |
| Module extension: `go_sdk`      | 1.14s     | Downloads SDK metadata    |
| Module extension: `go_deps`     | 0.63s     | Parses go.sum             |
| Module extension: `python.bzl`  | 0.59s     | Toolchain config          |
| Package loading + repo fetching | parallel  | 435 pkgs, 3002 repo links |

The 5.9s `queryEnv.evaluateQuery` contains all of the above running in
Skyframe's parallel evaluator. Starlark execution dominates (85.7s of
aggregated thread time across parallel threads), with `parse_modules` (pip
requirements parsing) and `_npm_lock_imports_bzlmod` (pnpm lock parsing)
being the heaviest individual functions.

### Repository fetching: 3002 npm link repos

The `Fetching repository` category accounts for 45.2s of aggregated thread
time (parallelized). These are `aspect_rules_js` npm package link
repositories — each one takes ~0.5-0.8s individually. They run in parallel
via Skyframe but are I/O-bound (creating symlink trees on disk).

### Package creation: 435 packages

Package creation accounts for 78.7s of aggregated thread time (parallelized).
The slowest packages are large directories: `tana` (1.36s), `cluster/k8s`
(1.36s), `homeassistant` (1.35s). These involve filesystem glob operations
to discover source files.

## Phase breakdown: subsequent warm queries (0.29–0.35s)

Once module extensions and packages are cached in Skyframe, queries are
dominated by **filesystem diff checking**:

| Phase                          | Wall time  | % of total |
| ------------------------------ | ---------- | ---------- |
| `fsvc.getDirtyKeys`            | 0.22–0.26s | ~75%       |
| `queryEnv.evaluateQuery`       | 0.02–0.04s | ~10%       |
| `Launch Blaze` (client→server) | 0.01s      | ~3%        |
| `QueryOutputUtils.output`      | 0.002–003s | ~1%        |

`fsvc.getDirtyKeys` (filesystem version cache) scans for modified files
since the last command. This is pure I/O — sequential `stat()` calls on
watched paths. On ext4 this takes ~0.25s; on 9p filesystems it would be
significantly slower.

The actual query evaluation (`function.eval/kind` or `function.eval/tests`)
takes 13–33ms once the package graph is cached.

## Cold start breakdown (11–29s)

| Component                    | First cold | Subsequent cold |
| ---------------------------- | ---------- | --------------- |
| JVM startup (`Launch Blaze`) | 11.6s      | ~5s             |
| Module extension eval        | ~7s        | ~3.4s           |
| Package loading              | parallel   | parallel        |
| **Total**                    | **29.2s**  | **11.2–13.2s**  |

The first cold start is ~29s because the JVM must fully initialize (11.6s)
and module extensions must re-evaluate from scratch (no Skyframe cache).
Subsequent cold starts are ~11s because Bazel's on-disk repository cache
persists across server restarts — only JVM startup and Skyframe
repopulation from the disk cache are needed.

## Bottleneck classification

| Scenario         | CPU-bound                       | I/O-bound              |
| ---------------- | ------------------------------- | ---------------------- |
| Cold start       | JVM startup (11s)               | Repo fetch (parallel)  |
| First warm query | Starlark eval (pip/npm parsing) | Repo link creation     |
| Subsequent warm  | Negligible                      | `getDirtyKeys` (0.25s) |

**The pre-commit hook's critical path is the first warm query** (~6s),
which is dominated by CPU-bound Starlark execution (pip/npm requirements
parsing). Once that's paid, subsequent queries are I/O-bound at ~0.3s
(filesystem dirty-key scanning).

## Implications for the pre-commit hook

1. **The session start hook's `bazel info` warmup** starts the JVM but does
   **not** evaluate module extensions — the first real query still pays ~6s.
   A more effective warmup would run a lightweight query like `tests(//...)`.
2. **Subsequent hook invocations** (same server) take ~0.3s for the query
   phase, which is fast enough for interactive use.
3. **The `getDirtyKeys` I/O cost** (0.25s) is the floor for warm queries.
   On 9p filesystems (some container environments) this could be much worse.
