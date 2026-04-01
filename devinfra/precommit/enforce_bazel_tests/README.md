# enforce-bazel-tests

Pre-commit hook that verifies Bazel tests affected by staged
changes are cached and passing before allowing a commit.

## How it works

`enforce_bazel_tests.py` discovers staged files via pygit2, converts them to
Bazel source labels, and finds affected test targets through a two-step query:

1. **Validate labels** (~1s warm): `kind("source file", //pkg_a:* + //pkg_b:*)`
   filters out files that exist on disk but aren't declared in any BUILD srcs.
2. **Find affected tests** (~3s warm):
   `kind(".*_test", rdeps(<universe>, set(<labels>)))` with
   `--universe_scope` excluding broken packages (`x/cotrl`).
   The `x/` directory is expanded into individual sub-packages so only the
   broken `x/cotrl` is excluded.

Then runs `bazel test --check_tests_up_to_date` on the affected targets
(no execution — just checks the local action cache). Requires
`--remote_download_minimal` in `.bazelrc` for RBE
([bazel#3978](https://github.com/bazelbuild/bazel/issues/3978)).

The core logic lives in `find_affected_tests()`, which takes candidate
`BazelLabel`s and returns affected test `BazelLabel`s. Both the hook and
the benchmark use it.

## Environment variable guard

The hook is guarded by `DUCKTAPE_PRECOMMIT_ENFORCE_BAZEL_TESTS=1` (default off).
The session start hook exports this variable for Claude Code sessions.

## Components

| File                     | Purpose                                             |
| ------------------------ | --------------------------------------------------- |
| `enforce_bazel_tests.py` | Pre-commit hook entry point + `find_affected_tests` |
| `bench.py`               | Cold/warm benchmark for query strategies            |

## Running the benchmark

```bash
bazel run //devinfra/precommit/enforce_bazel_tests:bench
bazel run //devinfra/precommit/enforce_bazel_tests:bench -- --profile  # with Bazel JSON trace profiles
```

Uses a separate `--output_base` so cold-start measurements don't conflict
with the parent `bazel run` server. Propagates session bazelrc startup flags
(proxy, TLS CA) to the bench's separate Bazel server. Results (stdout/stderr,
elapsed times, target lists) are saved to `/tmp/enforce_bazel_tests_bench/runs/<timestamp>/`.

## Benchmark results (2026-04-01, Bazel 8.6.0, ext4, gVisor)

Target file: `util/bazel/workspace.py` (`//util/bazel:workspace.py`).
Detailed profile analysis: <debug/warm_query_profile.md>.

### Cold start (server shut down before each query)

| Query                                 | Time   | Targets |
| ------------------------------------- | ------ | ------- |
| `kind("py_test", //...)`              | 29.15s | 305     |
| `kind("go_test", //...)`              | 11.50s | 3       |
| `kind(".*_test", //...)`              | 11.25s | 429     |
| `tests(//...)`                        | 13.23s | 429     |
| `kind("py_test", <scoped>)`           | 11.35s | 297     |
| `kind(".*_test", <scoped>)`           | 12.20s | 412     |
| `tests(<scoped>)`                     | 12.27s | 412     |
| `//...` (enumerate all)               | 11.20s | 8503    |
| `rdeps(//..., label)`                 | 34.28s | FAILED  |
| `somepath(kind(".*_test", //...), l)` | 57.67s | FAILED  |

### Warm server (no shutdown between queries)

| Query                    | Time  | Targets |
| ------------------------ | ----- | ------- |
| `kind("py_test", //...)` | 6.07s | 305     |
| `kind(".*_test", //...)` | 0.35s | 429     |
| `tests(//...)`           | 0.29s | 429     |

### Notes

- The first cold query is ~29s (JVM startup 11.6s + full module extension
  eval ~7s). Subsequent cold queries are ~11s (JVM startup only; on-disk
  repo cache persists across restarts).
- The first warm query pays ~6s for module extension evaluation (pip 3.4s,
  npm 2.2s, go_sdk 1.1s). Subsequent warm queries skip this entirely.
- Subsequent warm queries are ~0.3s, dominated by `fsvc.getDirtyKeys`
  (filesystem diff scanning, ~0.25s). Actual query evaluation is 13–33ms.
- **`rdeps(//..., ...)` is unusable** (~34s cold) because `//...`
  transitively loads broken external packages (gymnasium).
- Scoped queries return fewer targets (297/412 vs 305/429) because
  `_EXCLUDED_PACKAGES` filters out `x/cotrl` and `gterm_theme`.

## Known issues from development

- **Files not in BUILD srcs** cause query errors. Fixed by validating
  labels with `kind("source file", ...)` before the rdeps query.
- **`//...` universe loads broken external deps** (gymnasium, pygobject).
  Fixed by constructing a scoped universe that excludes `_EXCLUDED_PACKAGES`.
- **`//...` is slow for rdeps** (~21s warm). `--universe_scope` brings it
  to ~3s.
- **Root package label `//:*`** rejected by Bazel ("empty target name").
  Use `//:all` instead.
- **`bazel-*` convenience symlinks** in the repo root were traversed by
  `build_universe()`, causing `no targets found beneath 'bazel-ducktape'`
  errors in scoped queries. Fixed by filtering entries starting with `bazel-`.
