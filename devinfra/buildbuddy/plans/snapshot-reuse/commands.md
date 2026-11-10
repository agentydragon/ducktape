# Proposed commands — not executed

Use these only after separate explicit owner approval of the experiment and approval of [the plan](README.md). All three required checks on #14133 final head `ea45f152f642fafb3a779c62edf53a8d043cff08` succeeded; the PR was squash-merged as `52421a86166798d31941a11f988a30759ce51485` at `2026-11-04T01:26:26Z`. Use that exact merged commit as the trial input; any changed input requires renewed review. This preflight refresh does not authorize running these commands. They authorize no merge, deployment, manual cache invalidation or VM/KVM run. The unchanged lifecycle-error guard can invalidate only the isolated experimental family during these tests. Stop on any failed or unknown submission; no automatic retry. The only behavior under investigation is the existing default save policy on one isolated Pod family.

Tools must come from the repository Nix devshell, with normal BuildBuddy credentials. Run setup from a checkout containing these plan files. These commands are instructions, not an automatically executed workflow.

## Local setup after approval

```bash
set -euo pipefail
source_tree=$(git rev-parse --show-toplevel)
review_root="$source_tree/devinfra/buildbuddy/plans/snapshot-reuse"
task_root="${TMPDIR:-/tmp}/agents/$(date -u +%Y_%m_%d)/snapshot-policy-review"
trial_tree="$task_root/source"
trial_output="$task_root/results"

mkdir -p "$trial_output"
git -C "$source_tree" worktree add \
  -b codex/k3s-snapshot-policy-trial-20261103 \
  "$trial_tree" 52421a86166798d31941a11f988a30759ce51485
git -C "$trial_tree" apply "$review_root/trial.patch"
cd "$trial_tree"
pre-commit run \
  --files agentplane/system_tests/k3s/defs.bzl --show-diff-on-failure \
  > "$trial_output/precommit.log" 2>&1
git add agentplane/system_tests/k3s/defs.bzl
git commit \
  -m 'Isolate the approved default snapshot policy trial'
git rev-parse HEAD > "$trial_output/source-commit.txt"
sha256sum "$review_root/trial.patch" \
  > "$trial_output/patch-sha256.txt"
```

Before submission, read the effective workspace/home `.bbrc` and session `BBR_BAZELRC` settings for save-policy/header overrides without printing credentials. Verify the exact trial diff: one family prefix plus `test.retry=false`, no `always`, no `snapshot-key-override`, no read-policy/resource/fingerprint/test changes. The new experimental commit is not pushed as an implementation PR.

## Submit the seed once

```bash
test ! -e "$trial_output/seed.invocation-id"
test ! -e "$trial_output/seed.log"
bbr \
  --invocation-id-file="$trial_output/seed.invocation-id" \
  test --config=ci --nocache_test_results \
  --runs_per_test=1 --flaky_test_attempts=1 --test_output=errors \
  //agentplane/system_tests/suites:test_pod \
  > "$trial_output/seed.log" 2>&1
```

The ID file is written before remote execution. If the command disconnects, inspect that same invocation and execution rather than submitting again. Retrieve the native execution list, test artifacts and exact ExecuteResponse with the existing read-only interfaces:

```bash
seed_invocation=$(cat "$trial_output/seed.invocation-id")
bbapi --json execution "$seed_invocation" \
  > "$trial_output/seed-executions.json"
bbapi --json artifact list "$seed_invocation" \
  > "$trial_output/seed-artifacts.json"
```

Select the physical `TestRunner` row for this target, preserving all attempts; use its exact `executionId` with `bb execution get --output=json`. Download Action/Command using the advertised instance and digest algorithm. For inner ordinary RBE this was empty-instance SHA256 in the historical sample; the outer runner used `bb-snapshot` and BLAKE3. Do not substitute one namespace or action ID for another. Download the existing advertised persistence receipt, driver/process spans, transitions, logs and storage evidence through `bbapi`.

Review fourteen passing case identities, cleanup, fresh-state receipts, the actual new-policy request, fingerprint and lineage before continuing. The seed's `savedRemoteSnapshot` flag is an intent observation, not an upload-completed receipt.

## Submit the identical consumer once

Only after the seed gate passes, within the observed refresh interval, and with an unchanged source/patch:

```bash
test ! -e "$trial_output/consumer.invocation-id"
test ! -e "$trial_output/consumer.log"
bbr \
  --invocation-id-file="$trial_output/consumer.invocation-id" \
  test --config=ci --nocache_test_results \
  --runs_per_test=1 --flaky_test_attempts=1 --test_output=errors \
  //agentplane/system_tests/suites:test_pod \
  > "$trial_output/consumer.log" 2>&1
```

Collect the same evidence. Require a warm compatible baseline and new fixture identities, fourteen successful cases, complete cleanup and a false save-intent decision. Separate local attach proof from remote publication proof using the preceding task/snapshot lineage. If publication is unobserved or attachment is cold, retain that outcome and stop; do not keep running until warm.

Stop after the pair and report all outcomes, clocks and visibility limits. An aged-baseline test, VM family, additional repetition or provider message needs the next approval.
