# BuildBuddy per-target history is off for this repo (2026-08-03)

`bbapi target {history,stats,flakes}` return empty for ducktape, so the `buildbuddy_api` skill's
"bisect with target history instead of `git bisect`" recipe does not work here — localizing an
intermittent failure means re-running CI and watching. Found while chasing the CI timeouts in
<2026_08_rbe_small_test_timeouts.md>, but independent of them: that investigation is about test
sizing, this is about why it could not use the usual tool.

## Why

Reading the BuildBuddy source settles it, and it is **two** gates, not one. `server/build_event_protocol/target_tracker/target_tracker.go`
(both `handleWorkspaceStatusEvent` and `handleLastEvent`) checks, in order:

```go
if t.buildEventAccumulator.Invocation().GetRole() != "CI" { return }
if t.buildEventAccumulator.DisableTargetTracking()      { return }
```

and `enterprise/server/cmd/ci_runner/main.go` fails both for a `bb remote --script` run,
because `isWorkflow := *workflowID != ""` is false when there is no workflow id:

```go
if isWorkflow {                                   // false for us
    lines = append(lines, "common --build_metadata=ROLE=CI")
}
if !isWorkflow || *prNumber != 0 {                // true for us
    lines = append(lines, "common --build_metadata=DISABLE_TARGET_TRACKING=true")
}
```

so the invocation gets `ROLE=HOSTED_BAZEL` (`main.go` sets that on the else branch) _and_
the disable flag. **Overriding only the disable flag is a no-op — the role gate is checked
first.** Both have to be set.

The value _is_ parsed rather than merely tested for presence — `accumulator.getBoolValue`
accepts only `true/True/TRUE/yes/1` — so `=false` genuinely re-enables. And a command-line
`--build_metadata` beats the runner's generated `buildbuddy.bazelrc`, with Bazel deduping
repeated keys last-wins.

Verified by experiment rather than by reading alone. `bbr test //finance/augur/sim:tax_test`
with `--build_metadata=ROLE=CI --build_metadata=DISABLE_TARGET_TRACKING=false` plus
repo/commit/branch metadata (invocation `c8df8371-dfe6-4fa8-9aa5-af321d46989b`):

```text
$ bbapi target history --repo https://github.com/agentydragon/ducktape --label //finance/augur/sim:tax_test
Target: //finance/augur/sim:tax_test
STATUS  DUR  STARTED           COMMIT    INVOCATION
PASSED  11s  2026-08-03 22:34  8ea4623f  6be50921-c5ae-4280-9b23-25c46dcdc9dd
```

Control: `//finance/augur/fit:private_equity_test`, run many times without the flags, still
returns nothing.

`ROLE == "CI"` gates nothing else server-side — the target tracker is its only consumer —
and commit-status reporting stays off via the runner's own
`DISABLE_COMMIT_STATUS_REPORTING=true`.

Until this lands, the `buildbuddy_api` skill's "bisect with target history instead of
`git bisect`" recipe does not work against ducktape CI.

## What BuildBuddy intends, and what we do instead

Reading the two emissions as a table makes the design obvious:

|                          | `prNumber == 0`                        | `prNumber != 0`                        |
| ------------------------ | -------------------------------------- | -------------------------------------- |
| **workflow**             | `ROLE=CI`, no disable → **tracked**    | `ROLE=CI` + disable → not tracked      |
| **`bb remote --script`** | `HOSTED_BAZEL` + disable → not tracked | `HOSTED_BAZEL` + disable → not tracked |

Tracking is on in exactly **one** cell — a workflow run on a non-PR event — and for a `--script`
run _both_ gates fail independently. That redundancy is the tell that the exclusion is deliberate
rather than an oversight: a PR builds an unmerged commit whose per-target results are not part of
mainline history, and `bb remote --script` is classified as _hosted bazel_, which carries no
promise of being a clean mainline commit at all. Indexing either would make "when did this target
start failing" answer the wrong question.

The sanctioned way to get target history is therefore to run CI as a BuildBuddy **workflow**.
ducktape deliberately does not (`AGENTS.md`: all CI runs through GitHub Actions → `bbr`, no
`buildbuddy.yaml`), so this override exists to reproduce the tracked cell without adopting
workflows — **not** to assert CI-ness for every remote build.

Hence the condition: the metadata is emitted only on `push`, which is exactly that cell.
`ci.yml` already restricts pushes to `main`/`devel`, so no branch match is needed, and
`pull_request` / `pull_request_target` / `workflow_dispatch` all fall through. Making the flags
unconditional would index PR commits into mainline history — precisely the noise BuildBuddy's own
`prNumber` check exists to prevent — and would widen the `ROLE=CI` assertion, whose only safety
argument is that the target tracker is its sole server-side consumer _today_.

**Known cost:** no per-target history for PR runs, so a test's duration trend is visible on
`devel` after merge rather than on the PR before it. That is the trade BuildBuddy itself makes.
