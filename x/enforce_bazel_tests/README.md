# enforce-bazel-tests

Experimental pre-commit hook that verifies Bazel tests affected by staged
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

## Components

| File                     | Purpose                                             |
| ------------------------ | --------------------------------------------------- |
| `enforce_bazel_tests.py` | Pre-commit hook entry point + `find_affected_tests` |
| `bench.py`               | Cold/warm benchmark for query strategies            |

## Running the benchmark

```bash
bazel run //x/enforce_bazel_tests:bench
bazel run //x/enforce_bazel_tests:bench -- --profile  # with Bazel JSON trace profiles
```

Uses a separate `--output_base` so cold-start measurements don't conflict
with the parent `bazel run` server. Propagates session bazelrc startup flags
(proxy, TLS CA) to the bench's separate Bazel server. Results (stdout/stderr,
elapsed times, target lists) are saved to `/tmp/enforce_bazel_tests_bench/runs/<timestamp>/`.

## Benchmark results

Measured on Claude Code web (gVisor container, Bazel 8.5.0).

### Cold start (each query from `bazel shutdown`)

| Query                                       |    Time | Targets | Status                     |
| ------------------------------------------- | ------: | ------: | -------------------------- |
| `kind("py_test", //...)`                    | ~98s \* |     305 | OK (slow first — BCR init) |
| `kind("go_test", //...)`                    |    ~31s |      13 | OK                         |
| `kind(".*_test", //...)`                    |    ~31s |     461 | OK                         |
| `tests(//...)`                              |    ~31s |     461 | OK                         |
| `kind("py_test", <scoped>)`                 |    ~32s |     227 | OK                         |
| `kind(".*_test", <scoped>)`                 |    ~32s |     369 | OK                         |
| `tests(<scoped>)`                           |    ~31s |     369 | OK                         |
| `//...` (enumerate all targets)             |    ~31s |   8,440 | OK                         |
| `rdeps(//..., <label>)`                     |   ~264s |       — | FAILED (exit 7)            |
| `somepath(kind(".*_test", //...), <label>)` |   ~171s |       — | FAILED (exit 7)            |
| `kind(".*_test", allrdeps(<label>))`        |    ~25s |       — | FAILED (exit 2)            |

\* First query after fresh output base initialization includes BCR module
resolution overhead. Subsequent cold starts are ~31s.

### Warm server

| Query                                |   Time | Targets | Status          |
| ------------------------------------ | -----: | ------: | --------------- |
| `kind("py_test", //...)`             |  5.10s |     305 | OK              |
| `kind(".*_test", //...)`             |  0.34s |     461 | OK              |
| `tests(//...)`                       |  0.27s |     461 | OK              |
| `rdeps(//..., <label>)`              | 20.78s |       — | FAILED (exit 7) |
| `kind(".*_test", allrdeps(<label>))` |  0.39s |       — | FAILED (exit 2) |

### Cold-start profile breakdown

Profiled with `--generate_json_trace_profile`. All measurements from Claude
Code web (gVisor container) — gVisor intercepts every syscall, which
dramatically inflates JVM startup. Expect ~2–3s JVM startup on bare metal.

| Phase                            |  Time | Notes                                            |
| -------------------------------- | ----: | ------------------------------------------------ |
| **JVM startup ("Launch Blaze")** | 21.1s | 68% of wall time — gVisor syscall overhead       |
| Module ext: `pip.bzl%pip`        |  2.9s | Parsing `requirements_bazel.txt`, creating repos |
| Module ext: `npm:extensions%npm` |  1.6s | Parsing `pnpm-lock.yaml`                         |
| Module ext: `go_deps`            |  1.1s | Resolving Go module graph                        |
| Module ext: `go_sdk`             |  1.0s | Go SDK setup                                     |
| Module ext: `python.bzl%python`  |  0.4s | Python toolchain                                 |
| Repository fetches (npm links)   | ~138s | 3000 repos, ~1s each, heavily parallelized       |
| Package creation (BUILD loading) | ~110s | 671 packages, heavily parallelized               |

The ~31s wall time is dominated by JVM startup (21s) + module extension
evaluation (~7s) + parallelized package loading (~3s on the critical path).

### Key observations

- **`tests(//...)` is the fastest warm-server option** — 0.27s vs 0.34s for
  `kind(".*_test", //...)`. Functionally equivalent (both return 461 targets).
- **Cold-start is ~31s regardless of query** — dominated by JVM startup
  (21s under gVisor) + module extension evaluation. Query complexity adds
  negligible overhead. On bare metal, expect ~12–15s cold start.
- **No query optimization can fix cold start** — the bottleneck is server
  boot, not query evaluation. A persistent Bazel server is the only fix.
- **Scoped queries return fewer targets** (369 vs 461) because they exclude
  broken packages (`x/cotrl`). No speed benefit for cold start.
- **`rdeps` and `somepath` fail** with `//...` universe due to `gymnasium`
  (missing pip package). They need scoped universes.
- **`kind("py_test", //...)`** is anomalously slow on first warm-server call
  (~5s vs ~0.3s for subsequent queries), likely due to regex compilation
  or internal caching effects.

## Current status

Not integrated into pre-commit. The hook is commented out in
`.pre-commit-config.yaml`. The main blocker was `language: python`
isolation — pre-commit's virtualenv can't access shared repo modules or
the Bazel output base. Switching to `language: system` would fix the
import issue but hasn't been done yet.

Performance with a warm Bazel server (~4s total) is acceptable. Cold
start (~31s+) is not — this dominates commit latency if the Bazel server
isn't already running.

## Known issues from development

- **Files not in BUILD srcs** cause query errors. Fixed by validating
  labels with `kind("source file", ...)` before the rdeps query.
- **`//...` universe loads broken external deps** (gymnasium, pygobject).
  Fixed by constructing a scoped universe that excludes `_EXCLUDED_PACKAGES`.
  `gterm_theme` (pygobject/pycairo) was fixed by installing the correct
  native dev packages (`libgirepository-2.0-dev`, `libcairo2-dev`,
  `libdbus-1-dev` — 3 packages, ~2.2 MB, ~7s install on Claude Code web).
  Only `x/cotrl` (gymnasium) remains excluded.
- **`//...` is slow for rdeps** (~21s warm). `--universe_scope` brings it
  to ~3s.
- **Root package label `//:*`** rejected by Bazel ("empty target name").
  Use `//:all` instead.
- **Separate `--output_base` needs session startup flags** — when running
  under `bazel run`, the bench's separate Bazel server needs the proxy and
  TLS CA JVM args from the session bazelrc to reach BCR.

## Next steps

- Evaluate whether cold-start latency can be reduced (persistent Bazel
  server, different query strategy, or only running when the server is
  already warm).
- Consider alternative approaches: `bazel-diff` for affected target
  discovery (already used in CI), or a lighter-weight file-to-target
  mapping that avoids `bazel query` entirely.
- If viable, integrate into `devinfra/precommit/precommit.py` via
  `language: system`. Otherwise, remove.
