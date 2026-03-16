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
   `--universe_scope` excluding broken packages (`x`, `gterm_theme`,
   `bazel-ducktape`).

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
| `bench.py`               | Cold-start benchmark for query strategies           |

## Running the benchmark

```bash
bazel run //x/enforce_bazel_tests:bench
```

Shuts down the Bazel server, makes a temporary change to
`util/bazel/workspace.py`, and measures total time to discover affected
tests from a cold start. Also benchmarks simpler query strategies
(`kind("py_test", //...)`, etc.) for comparison.

## Benchmark results

Measured on Claude Code web (gVisor container, Bazel 8.5.0, cold start
each measurement — `bazel shutdown` before every query):

| Query                                       |    Time | Targets |
| ------------------------------------------- | ------: | ------: |
| `find_affected_tests` (scoped rdeps)        | ~237s\* |   n/a\* |
| `kind("py_test", //...)`                    |    ~35s |     302 |
| `kind("go_test", //...)`                    |    ~34s |       8 |
| `kind(".*_test", //...)`                    |    ~33s |     447 |
| `//...` (enumerate all targets)             |    ~34s |   8,388 |
| `rdeps(//..., <label>)`                     |  ~56s\* |   n/a\* |
| `somepath(kind(".*_test", //...), <label>)` |     TBD |     TBD |
| `kind(".*_test", allrdeps(<label>))`        |     TBD |     TBD |

\* `find_affected_tests` and unscoped `rdeps` fail (exit 7) due to broken
external deps in `//...` universe — the scoped universe excludes these, but
still times out on cold start. With a warm server, scoped rdeps takes ~3s.

`somepath` and `allrdeps` are alternative strategies being evaluated — they
find tests first and then check for transitive dependency paths, rather than
computing rdeps over the full universe.

**Key takeaway**: Cold-start query time is ~33–35s regardless of query
complexity (dominated by Bazel server startup + BCR resolution). The
`find_affected_tests` scoped rdeps approach adds negligible overhead on a
warm server but is impractical from cold start.

## Current status

Not integrated into pre-commit. The hook is commented out in
`.pre-commit-config.yaml`. The main blocker was `language: python`
isolation — pre-commit's virtualenv can't access shared repo modules or
the Bazel output base. Switching to `language: system` would fix the
import issue but hasn't been done yet.

Performance with a warm Bazel server (~4s total) is acceptable. Cold
start (~33s+) is not — this dominates commit latency if the Bazel server
isn't already running.

## Known issues from development

- **Files not in BUILD srcs** cause query errors. Fixed by validating
  labels with `kind("source file", ...)` before the rdeps query.
- **`//...` universe loads broken external deps** (gymnasium, pygobject).
  Fixed by constructing a scoped universe that excludes `_EXCLUDED_PACKAGES`.
- **`//...` is slow for rdeps** (~28s warm). `--universe_scope` brings it
  to ~3s.
- **Root package label `//:*`** rejected by Bazel ("empty target name").
  Use `//:all` instead.

## Next steps

- Evaluate whether cold-start latency can be reduced (persistent Bazel
  server, different query strategy, or only running when the server is
  already warm).
- Consider alternative approaches: `bazel-diff` for affected target
  discovery (already used in CI), or a lighter-weight file-to-target
  mapping that avoids `bazel query` entirely.
- If viable, integrate into `devinfra/precommit/precommit.py` via
  `language: system`. Otherwise, remove.
