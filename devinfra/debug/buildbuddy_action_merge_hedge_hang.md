# BuildBuddy action merging: a hedged execution's result never reaches the merged client

**Status:** active; the upstream report is drafted below and not yet filed. Observed 2026-10-04/05.

## Symptom

A `bazel-ci` job stops making progress with every test passed and one action left in `[Sched]`.
Its elapsed time climbs until BuildBuddy kills the remote run at its 1-hour cap:

```text
[10,085 / 10,111] 23 / 23 tests; [Sched] Transpiling & type-checking TypeScript project @@//agentplane/app/frontend/threads:thread_cards [...]; 3462s
ERROR: Remote run exceeded timeout (1h0m0s) due to free tier limitations. Contact support@buildbuddy.io to upgrade your plan.
Command failed: signal: killed
```

`timeout-minutes: 60` in `.github/workflows/bazel-ci.yml` fires at the same time and cancels the job.
`--remote_timeout=10m` (`devinfra/bazel/rbe.bazelrc`) does not bound the wait.

Re-running the job passed in about 3 minutes (observed once): the hedged duplicate had already written the action's result to the action cache.

## Recognizing it

```bash
bbapi --json execution <test-invocation-id>
```

One row per execution. The signature is an execution whose `stage` is not `COMPLETED`, plus a second execution of the same `actionDigest`, `COMPLETED`, in the same invocation:

| Row                                   | `stage`                                            | `invocationLinkType`  |
| ------------------------------------- | -------------------------------------------------- | --------------------- |
| Original, stuck                       | `CACHE_CHECK` or `EXECUTING`                       | `2` (`MERGED`)        |
| Hedge, finished (same `actionDigest`) | `COMPLETED`                                        | `1` (`NEW`)           |
| Originating invocation's own row      | stuck; `queuedTimestamp` `1970-01-01` if never run | `1` (`NEW`), no hedge |

Gotchas when reading the evidence:

- `bbapi cache <invocation>` shows only the first 100 scorecard rows. Page `GetCacheScoreCard` through the Twirp API (`pageToken`) to find an action's `READ` miss time.
- The Actions step log trails the runner's output: by the kill it showed tick 2022 s where the runner had reached 3462 s, about 24 minutes behind, draining about one line a minute. The runner invocation's log has the real timing; its ID is on the `Streaming remote runner logs to:` line of the step log (`bb view <id>`). The cause of the lag is unknown.
- `bb execution get <execution-id>` answers `NotFound` for an execution no executor ever claimed: no response was stored.

## Mechanism

Read from `buildbuddy-io/buildbuddy` `master` on 2026-10-05, git blob `b48fdb28` (`enterprise/server/remote_execution/execution_server/execution_server.go`) and `0384bd8a` (`.../action_merger/action_merger.go`). The cloud's version and flags are not visible from here.

1. `ExecutionServer.Execute` calls `action_merger.GetOrCreateExecutionID`, which returns `New`, `Merge` or `Hedge`. Merging is on by default; hedging is off by default (`--remote_execution.action_merging_hedge_count` is `0`, `--remote_execution.action_merging_hedge_delay` is `0s`).
2. For `Merge` and `Hedge`, Execute inserts a `MERGED` invocation link on the existing execution and calls `waitExecution` on the **existing** execution ID.
3. For `Hedge`, Execute also calls `dispatchHedge`: a second execution under a fresh ID (`NewUploadString`), whose `dispatch` inserts a `NEW` link.
4. `waitExecution` reads only `pubSubChannelForExecutionID(<the ID it was given>)`. It polls no cache and has no timeout of its own.
5. `PublishOperation` publishes each operation to the channel of the task that produced it. The hedge's completion goes to the hedge's channel; no path in either file forwards it to the original's. The hedge's `ActionResult` reaches the action cache, so only later requests see it.

So a request that merges into a stuck original waits on a channel that nothing will publish to, however quickly the hedge finishes. With the signature above, hedging is on in the cloud and fired on the first merge, when the original was already 62 minutes old (attempt 2) or 40 minutes old (`bd16e59d`).

## Evidence

All five invocations ended `DISCONNECTED`. Scan window 2026-10-04 19:51 to 2026-10-05 03:33 UTC: 2,986 inner invocations, about 45,800 executions, 3 stuck (roughly 1 in 15,000).

| Invocation (`test`/`build`)            | Stuck execution                                                                             | Hedge twin                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------- |
| `8945174a-a58b-504e-96b4-3ba2cf564db2` | `fd4aec8d` `TsProject //agentplane/app/frontend/threads:thread_cards`, `CACHE_CHECK`, `NEW` | none                            |
| `65234ce0-9a5a-5008-9edf-1b2174d534e7` | `fd4aec8d`, `MERGED`                                                                        | `bd0211cd`, `COMPLETED` in 25 s |
| `dd97ddd2-5af7-52df-bca2-0ea24cbd6169` | `0b19834e` `Rustc //devinfra/js/debundle/e2e:naturalization_test`, `CACHE_CHECK`, `NEW`     | none                            |
| `4d246247-8765-4a5e-a2a9-044bd94737c6` | `83e60aaf` `AspectRulesLintESLint //aiquota/frontend:page_css`, `EXECUTING`, `NEW`          | none                            |
| `bd16e59d-3d06-40ae-aac8-414d01df18ac` | `83e60aaf`, `MERGED`                                                                        | `23eab90b`, `COMPLETED`         |

The first two rows are attempts 1 and 2 of Actions run 37249915576; attempt 3 (`fae8fed2-6f93-5725-86c3-683f6405a6d6`) passed.

Timeline for `thread_cards` (action `0e083fdec0caf19435fae87c314d44cd0fa26ca5bf518bdf768290c8e1d20899/493`, UTC, 2026-10-05):

| Time           | Event                                                                                  |
| -------------- | -------------------------------------------------------------------------------------- |
| 01:06:06       | Attempt 1: action-cache miss, then `Execute` creates `fd4aec8d`, which is never queued |
| 02:08:50.96    | Attempt 2: action-cache miss; `Execute` merges into `fd4aec8d`                         |
| 02:08:52.19    | Hedge `bd0211cd` queued; worker finishes at 02:09:17.8                                 |
| 02:09 to 03:07 | Attempt 2's Bazel stays in `[Sched]`; the runner is killed at 03:07:24                 |
| 03:36:53       | Attempt 3: action-cache hit, `originInvocationId` is attempt 2's invocation            |

## Not established

- Why `fd4aec8d` and `0b19834e` were never claimed, and why `83e60aaf` stopped after an executor claimed it. Needs BuildBuddy's scheduler, executor and Redis state.
- Why `fd4aec8d` was still mergeable 62 minutes after creation: `queuedExecutionTTL` is 10 minutes, extended while the task is claimed.
- Whether the cloud runs the `master` code above and has hedging enabled by configuration (inferred only from the link-type signature).

## Upstream report (draft)

**Title:** Action merging with hedging: a client merged into a stuck execution never receives the hedge's result

**Summary.** When `Execute` merges a request into an existing execution that never completes, and a hedged execution is dispatched for it, the hedge completes and writes the action result to the action cache. The client stays attached to the original execution's pub/sub channel indefinitely and never receives the result.

**Observed.** Bazel 9.2.0 through `bb remote`, `--config=rbe`. Original execution `fd4aec8d-f7fc-451f-9377-011a70969a21`, stage `CACHE_CHECK`, never queued. The invocation `65234ce0-9a5a-5008-9edf-1b2174d534e7` has a `MERGED` link to it and a `NEW` link to hedge `bd0211cd-2ef0-4092-b585-632da301a387` (same action `0e083fdec0caf19435fae87c314d44cd0fa26ca5bf518bdf768290c8e1d20899/493`), which completed in 25 s. Bazel stayed in `[Sched]` for 58 minutes until the remote run was killed at the 1-hour limit. The same pair appears for `83e60aaf` (stuck `EXECUTING`) and hedge `23eab90b` in invocation `bd16e59d-3d06-40ae-aac8-414d01df18ac`.

**Cause as read from `master`.** `Execute` waits on the merged execution's ID, `dispatchHedge` uses a fresh ID, `PublishOperation` publishes to the producing task's own channel, and `waitExecution` listens only on the ID it was given. Nothing connects the hedge's channel to the original's.

**Expected.** The merged client receives the first completion, from the original or from a hedge.

**Possible directions.** Publish a hedge's completion to the original execution's channel as well; or have a merged `waitExecution` also subscribe to its hedges; or decline to merge into an execution with no executor heartbeat. Separately, a stuck original stays mergeable far longer than the 10-minute queued TTL.

**Impact.** One stuck execution poisons every later request for the same action while its merge entry lives; we saw 5 invocations affected in under 8 hours. Re-running works because the hedge's result is in the action cache.
