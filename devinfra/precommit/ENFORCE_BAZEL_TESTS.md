# enforce-bazel-tests: Pre-commit Hook for Bazel Test Cache

## Status: WIP — needs integration into unified precommit.py

## What it does

Verifies that Bazel tests affected by staged changes have been run (cached and passing)
before allowing a commit. Prevents committing code without running tests.

Uses `bazel test --check_tests_up_to_date` which checks the local action cache without
executing anything. Works with RBE because `build:rbe --remote_download_minimal` in
`.bazelrc` downloads test results locally ([bazel#3978](https://github.com/bazelbuild/bazel/issues/3978)).

## Current state: standalone script (broken)

`enforce_bazel_tests.py` is a standalone script with `language: python` in pre-commit.
This doesn't work because pre-commit's isolated virtualenv can't access the bazel output
base directory. The script works when run directly (`python3 enforce_bazel_tests.py`)
but fails as a pre-commit hook.

## Two-step query approach (tested, working)

1. **Validate labels** (~1s warm): `kind("source file", //pkg_a:* + //pkg_b:* + ...)`
   filters out files that exist on disk but aren't declared in any BUILD srcs.
2. **Find affected tests** (~3s warm): `kind(".*_test", rdeps(<universe>, set(<labels>)))`
   with universe excluding broken packages (`x`, `gterm_theme`, `bazel-ducktape`).

Total: ~4s warm, ~68s cold (dominated by bazel server startup).

## Issues encountered

### 1. Pre-commit `language: python` isolation

Pre-commit creates an isolated virtualenv for `language: python` hooks. The virtualenv
can't access the bazel output base (`FATAL: Output base directory must be readable and
writable`). This is the fundamental blocker for the standalone approach.

### 2. Files not in BUILD srcs cause query errors

Files like `.pre-commit-config.yaml` exist in a Bazel package (root) but aren't declared
as source targets. `bazel query` errors on `//:.pre-commit-config.yaml`. Fixed by the
two-step approach: validate labels first with `kind("source file", ...)`.

### 3. `--keep_going` masks real errors

Initially used `--keep_going` to tolerate invalid labels, but this also masks errors from
broken BUILD files in staged changes — exactly the kind of error we want to catch.
Removed in favor of label validation.

### 4. `//...` universe loads broken external deps

Packages `x/cotrl` (gymnasium) and `gterm_theme` (pygobject/pycairo) have external deps
that fail at repo fetch time without system native libraries. Even with `tags = ["manual"]`,
`//...` still loads their packages. Fixed by constructing a scoped universe that excludes
these packages.

### 5. `//...` is slow (~28s) for rdeps

Using `//...` as the rdeps universe takes ~28s even warm. Scoping to specific top-level
dirs with `--universe_scope` brings it down to ~3s.

### 6. Root package label `//:*` rejected

Bazel rejects `//:*` with "invalid target name: empty target name". Fixed by using
`//:all` for the root package in the universe expression.

## Next steps: merge into precommit.py

See the plan in the PR description. Key changes:

- New `check_enforce_bazel_tests.py` validator module
- Uses `util.bazel.query.run_query` directly (no inlining)
- Runs via `run_precommit.sh` wrapper (language: system, no virtualenv isolation)
- Add `always_run: true` to bazel-precommit hook
- Set `PRECOMMIT_HOOK=1` env var in `run_precommit.sh` to distinguish from manual invocation
