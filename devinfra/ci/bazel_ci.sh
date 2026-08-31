#!/usr/bin/env bash
# The ducktape CI build: bazel test + build via `bb remote`, plus the bazel-diff
# affected-target selection for pull requests.
#
# Lives here rather than inline in .github/workflows/bazel-ci.yml because it is ~200 lines
# of bash with three functions, and a script embedded in a YAML string is invisible to
# shfmt and shellcheck (STYLE.md: "any embedded script/config block longer than ~5 lines
# lives in its native file"). It was 58% of that workflow file.
#
# EXECUTION CONTEXT — this does NOT run on the GitHub Actions runner. `bb remote --script`
# ships it to a BuildBuddy runner VM that has the repo checked out, which is why relative
# paths like devinfra/ci/bb_runner_probe.py resolve. It also means the ambient GitHub
# Actions environment is ABSENT: none of the usual GITHUB_* variables exist unless
# explicitly forwarded.
#
# Inputs, all forwarded from bazel-ci.yml via the bb-remote action's `env_overrides`
# (which become x-buildbuddy-platform.env-overrides headers), NOT ambient:
#
#   GITHUB_EVENT_NAME  push / pull_request / pull_request_target / workflow_dispatch.
#                      Gates the target-tracking metadata — see below.
#   PR_HEAD_SHA        trusted PR head; empty on non-PR events, which selects the `//...`
#                      path instead of bazel-diff.
#   PR_BASE_SHA        PR base at event time; advisory only (the real base is the merge
#                      commit's ^1 parent).
#   RBE_IMAGE          forwarded automatically by the bb-remote action itself.
#   TEST_INVOCATION_ID BuildBuddy invocation ID for the `bazel test` below, and
#   BUILD_INVOCATION_ID  likewise for `bazel build`. Derived from the run's identity by
#                      devinfra/ci/invocation_ids.py so a consumer can name the invocation
#                      without observing this script — see that module.

set -euo pipefail
# BuildBuddy's per-target history (`bbapi target {history,stats,flakes}`) is off for
# this repo, so localizing an intermittent failure means re-running CI and watching
# rather than querying — and the `buildbuddy_api` skill's "bisect with target history
# instead of git bisect" recipe does not work here.
#
# Two gates in `target_tracker.go` both have to pass:
#
#   if Invocation().GetRole() != "CI"  { return }   <- checked FIRST
#   if DisableTargetTracking()         { return }
#
# and `enterprise/server/cmd/ci_runner/main.go` decides both from
# `isWorkflow := *workflowID != ""`, which is false for a `bb remote --script` run:
#
#   ROLE=CI                      emitted `if isWorkflow`              -> we get HOSTED_BAZEL
#   DISABLE_TARGET_TRACKING=true emitted `if !isWorkflow || prNumber` -> always set for us
#
# So tracking is on in exactly ONE cell of that table: a WORKFLOW run on a NON-PR
# event. Everything else is deliberately excluded — a PR builds an unmerged commit,
# and a `--script` run is classified as hosted bazel, which carries no promise of
# being a clean mainline commit at all.
#
# We therefore reproduce that one cell rather than asserting ROLE=CI unconditionally.
# `push` is exactly it: ci.yml restricts pushes to main/devel, so no branch match is
# needed, while pull_request / pull_request_target / workflow_dispatch all fall
# through. Do NOT "simplify" this to an unconditional flag — that indexes PR commits
# into mainline target history, which is the noise BuildBuddy's own `prNumber` check
# exists to prevent.
#
# Overriding only the DISABLE flag is a no-op (the role gate is checked first).
# `false` is honored because the server PARSES the value — `getBoolValue` accepts only
# true/True/TRUE/yes/1 — rather than testing for the key's presence, and a
# command-line `--build_metadata` beats the runner's generated buildbuddy.bazelrc
# (Bazel dedupes repeated keys last-wins). ROLE=CI gates nothing else server-side:
# the target tracker is its only consumer, and commit-status reporting stays off via
# the runner's own DISABLE_COMMIT_STATUS_REPORTING.
TRACKING_FLAGS=""
if [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
  TRACKING_FLAGS="--build_metadata=ROLE=CI --build_metadata=DISABLE_TARGET_TRACKING=false"
fi
RBE_FLAGS="--config=rbe --config=ci $TRACKING_FLAGS --remote_default_exec_properties=container-image=docker://$RBE_IMAGE"
export CI_VM_PROBE_DIR="/home/buildbuddy/workspace/.ducktape-ci-vm-probe"
probe_bb_runner() {
  python3 devinfra/ci/bb_runner_probe.py snapshot "$1" || true
}
finalize_bb_runner_probe() {
  python3 devinfra/ci/bb_runner_probe.py finalize --upload || true
}
trap finalize_bb_runner_probe EXIT

# On PRs, test/build only the targets bazel-diff reports as
# affected by the diff from the merge commit's base parent to the
# synthetic merge tree. Devel-branch push runs
# below still test `//...`, so a PR that IS in the affected set
# of a broken area still fails as it should; PRs unrelated to
# the broken area are unblocked.
#
# `bazel-diff` binary comes from the RBE image via
# nix/packages/bazel-diff.nix (pinned to v16.0.0).
#
# Fails hard on any bazel-diff error — no fallback to `//...`.
# bazel-diff is the source of truth for the affected set; a
# silent fallback would let a broken-devel failure re-block
# unrelated PRs, defeating the whole policy.
PR_HEAD_SHA="${PR_HEAD_SHA:-}"
PR_BASE_SHA="${PR_BASE_SHA:-}"
if [ -n "$PR_HEAD_SHA" ]; then
  # bb-remote initially fetches the exact commit at depth 1, which
  # makes Git treat the synthetic merge as a parentless shallow
  # root. Fetch the same exact SHA at depth 2 so both merge parents
  # are available without downloading unrelated branch history.
  HEAD_SHA=$(git rev-parse HEAD)
  git fetch --depth=2 --no-tags origin "$HEAD_SHA"

  if ! BASE=$(git rev-parse "${HEAD_SHA}^1" 2>/dev/null) \
    || ! PR_HEAD=$(git rev-parse "${HEAD_SHA}^2" 2>/dev/null); then
    echo "::error::pull_request CI expected github.sha to be a two-parent synthetic merge commit"
    exit 1
  fi
  # Only the HEAD parent (^2) must match the trusted/event head. It
  # guards against a stale or subsequently advanced merge ref — we'd
  # otherwise test code different from the revision the caller trusted.
  if [ "$PR_HEAD" != "$PR_HEAD_SHA" ]; then
    echo "::error::synthetic merge head parent ($PR_HEAD) != trusted PR head ($PR_HEAD_SHA)"
    exit 1
  fi
  # The BASE parent (^1) is whatever devel was when GitHub built the merge.
  # It legitimately drifts ahead of github.event...base.sha when devel
  # advances between the event firing and merge generation (frequent given
  # devel churn), and ^1 is still the correct bazel-diff base: diffing
  # ^1 -> merge yields exactly the PR's delta either way. So a drifted base is
  # fine and must not fail CI — note it for visibility only.
  if [ -n "$PR_BASE_SHA" ] && [ "$BASE" != "$PR_BASE_SHA" ]; then
    echo "::notice::base advanced since the PR event (event base $PR_BASE_SHA -> merge base $BASE); diffing against the merge's base parent, which is correct"
  fi
  echo "before-revision: $BASE"
  echo "pr-head:         $PR_HEAD"
  echo "merge:           $HEAD_SHA"

  mkdir -p /tmp/bd-cache
  # `bazel query` (not cquery) — cheap: no analysis, no toolchain
  # resolution. Peak RAM ~1-2 GB per revision.
  git -c advice.detachedHead=false checkout --quiet "$BASE"
  bazel-diff generate-hashes -w "$PWD" -b bazel /tmp/bd-cache/base.json
  git -c advice.detachedHead=false checkout --quiet "$HEAD_SHA"
  bazel-diff generate-hashes -w "$PWD" -b bazel /tmp/bd-cache/head.json
  bazel-diff get-impacted-targets \
    -sh /tmp/bd-cache/base.json -fh /tmp/bd-cache/head.json \
    >/tmp/affected-raw.txt

  # bazel-diff's impacted set includes plain source-file labels
  # (e.g. a .py file referenced from a BUILD file but not wrapped
  # in a rule). `bazel build`/`bazel test` treat those as a
  # silent no-op ("is a source file, nothing will be built for
  # it") -- harmless but noisy. Filter them out up front so the
  # affected set passed to build/test only contains rules.
  #
  # Explicit labels do not get Bazel's normal wildcard semantics:
  # unlike `//...`, an explicit manual target is still selected.
  # Exclude manual-tagged targets here so the affected target file
  # has the same behavior as the normal wildcard target expansion.
  #
  # Each label is double-quoted inside set(...): aspect_rules_js
  # node_modules targets for scoped pnpm packages are named with
  # a '+' (e.g. //:.aspect_rules_js/node_modules/@lezer+json@1.0.3/dir),
  # and an unquoted '+' in a set() expression is parsed as the
  # set-union operator, so a PR that newly affects such a target
  # (any PR adding a scoped npm dep) breaks the query with a
  # syntax error. Quoting makes the '+' a literal label character.
  #
  # TODO: this set()/except/tests() query shuffling in shell is brittle; consider a
  # small python helper that takes affected-raw.txt and writes the resolved affected
  # set + test-query files directly, instead of building query expressions in shell.
  quote() { sed '/^$/d; s/.*/"&"/' "$1" | tr '\n' ' '; }
  {
    printf 'set('
    quote /tmp/affected-raw.txt
    printf ') except kind("source file", set('
    quote /tmp/affected-raw.txt
    printf ')) except attr("tags", "manual", set('
    quote /tmp/affected-raw.txt
    printf '))\n'
  } \
    >/tmp/affected-query.txt
  bazel query --query_file=/tmp/affected-query.txt >/tmp/affected.txt

  N=$(wc -l </tmp/affected.txt)
  echo "affected targets: $N"
  if [ "$N" -eq 0 ]; then
    echo "No targets affected by this PR — skipping test/build."
    exit 0
  fi
  TARGETS="--target_pattern_file=/tmp/affected.txt"
  # `bazel query` has no --target_pattern_file flag (only
  # build/test/etc. do), so the affected set can't be handed to
  # `tests(...)` the same way it's handed to `bazel test`.
  # set(...) embeds the same patterns in the query expression
  # instead, written to a file (--query_file) rather than argv
  # so a large affected set can't hit command-line length limits.
  {
    printf 'tests(set('
    quote /tmp/affected.txt
    printf '))\n'
  } \
    >/tmp/test-query.txt
else
  # push to devel / workflow_dispatch: full sweep.
  TARGETS="//..."
  echo 'tests(//...)' >/tmp/test-query.txt
fi

# `bazel test` requires the resolved pattern to include at least
# one *_test rule -- unlike `bazel build`, it hard-fails ("No
# test targets were found, yet testing was requested", exit 4)
# if the affected set is all non-test files (e.g. a PR that only
# touches BUILD-file-exported source/patch files). tests(...)
# is Bazel's own query for "just the test rules in this target
# set" (it expands test_suite too), so this check stays exactly
# in sync with what `bazel test` would itself select. stderr is
# left unredirected so a genuine query failure is still visible
# in the CI log, not just its (fail-hard, per set -e) exit code.
TEST_TARGET_COUNT=$(bazel query --query_file=/tmp/test-query.txt | wc -l)
echo "test targets in scope: $TEST_TARGET_COUNT"

probe_bb_runner before-test
# BuildBuddy runner VMs can be reused between invocations.  The probe above
# records that state for diagnosis; do not let a server from a previous
# checkout serve this test run.
bazel shutdown || true
if [ "$TEST_TARGET_COUNT" -eq 0 ]; then
  echo "No test targets in scope -- skipping bazel test."
  bazel build --invocation_id="$BUILD_INVOCATION_ID" --keep_going $RBE_FLAGS $TARGETS \
    && probe_bb_runner after-build
else
  # `bazel test` still hard-fails with exit 4 ("no test targets were
  # found") when every test in the affected set is filtered out by
  # --test_tag_filters -- e.g. a PR that only touches `manual`-tagged
  # tests. The TEST_TARGET_COUNT probe above counts test *rules* and
  # can't see tag filters, so it can't pre-empt this. Tolerate exit 4
  # ONLY on the PR affected-set path; on the full `//...` sweep it would
  # mean every test was filtered out -- a real problem we must not mask.
  test_rc=0
  bazel test --invocation_id="$TEST_INVOCATION_ID" --keep_going $RBE_FLAGS $TARGETS || test_rc=$?
  if [ "$test_rc" -eq 4 ] && [ "$TARGETS" != "//..." ]; then
    echo "No runnable tests after tag filtering in the affected set; continuing to build."
    test_rc=0
  fi
  if [ "$test_rc" -ne 0 ]; then
    exit "$test_rc"
  fi
  probe_bb_runner after-test \
    && bazel build --invocation_id="$BUILD_INVOCATION_ID" --keep_going $RBE_FLAGS $TARGETS \
    && probe_bb_runner after-build
fi
