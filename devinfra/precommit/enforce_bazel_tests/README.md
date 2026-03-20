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

## Known issues from development

- **Files not in BUILD srcs** cause query errors. Fixed by validating
  labels with `kind("source file", ...)` before the rdeps query.
- **`//...` universe loads broken external deps** (gymnasium, pygobject).
  Fixed by constructing a scoped universe that excludes `_EXCLUDED_PACKAGES`.
- **`//...` is slow for rdeps** (~21s warm). `--universe_scope` brings it
  to ~3s.
- **Root package label `//:*`** rejected by Bazel ("empty target name").
  Use `//:all` instead.
