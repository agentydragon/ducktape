# Claimed lease stream failures during Agentplane qualification

On 2026-11-14, two remote commands at the Agentplane architecture checkpoint
`9b330689a24042259393a713b5f271b42bd7e669` failed with
`wait execution: rpc error: code = Internal desc = stream closed with task still claimed`.
This is a provider/runner failure, distinct from a failing test assertion. Successful
component results do not make the failed aggregate command successful.

## Observed outcomes

| Exposure                                                                                                                                                                                      | Outcome                                                                                                                                                                                                                                                                                                                               |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Focused qualification](https://app.buildbuddy.io/invocation/12cdf1bb-c0de-400c-821e-8f79dcb4b275), [outer runner](https://app.buildbuddy.io/invocation/53504013-aa71-49c2-9f45-e5aa1cc6e284) | Both invocation records are disconnected. All seven exact TestRunner results were subsequently recovered: completed `ActionResult`s, zero action exit status, matching JUnit output digests, 59 passing cases and no failures/errors/skips.                                                                                           |
| [Hosted CI child](https://app.buildbuddy.io/invocation/7c7bf823-ac99-5fdd-a3fe-098a837df1b6), [outer runner](https://app.buildbuddy.io/invocation/085885e6-f11b-4e78-812b-896219dd3296)       | The outer execution is completed with RPC status 13 and the message above, without an `ActionResult` or worker execution timestamps. The child invocation is disconnected.                                                                                                                                                            |
| [Focused follow-up](https://app.buildbuddy.io/invocation/b741d170-3bb3-4980-946b-c1f5f1c55030)                                                                                                | A new command succeeds: 20 Pod17/MCP3 cases and eight explicitly named libraries with configured aspects. This is a separate exposure, not a correction of the earlier outcome.                                                                                                                                                       |
| [Hosted CI attempt 2](https://github.com/agentydragon/ducktape/actions/runs/43621238434/attempts/2)                                                                                           | One explicit failed-job rerun succeeds; all three required checks associated with the PR head pass. Both CI attempts test synthetic merge `2aec7a80cb38d483634e9236616168ba5e9a9a06`, with parents devel `ff801c85c732b368a67f0d6ada3bb487146abca8` and PR head `9b330689`. Direct focused commands test `9b330689` without overlays. |

The 59 recovered cases establish component correctness and the logical case
unions described in the [architecture evidence](../../../agentplane/system_tests/debug/common_backend_contracts.md).
They do not establish successful inner Bazel completion, successful outer runner
completion, or complete required feedback for the first attempt. Retain that failed
attempt and its elapsed time in reliability and feedback accounting. A recorded
test attempt is not an audit of executor-internal retries or rescheduling.

## Source mechanism and visibility limit

Clean upstream scheduler source at commit
[`f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962`](https://github.com/buildbuddy-io/buildbuddy/tree/f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962)
contains a matching mechanism. This source was inspected from a cached upstream
checkout; it does not identify the revision deployed by hosted BuildBuddy.

- [`LeaseTask` cleanup](https://github.com/buildbuddy-io/buildbuddy/blob/f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962/enterprise/server/scheduling/scheduler_server/scheduler_server.go#L2420-L2457)
  uses that literal reason when its stream closes while the task remains claimed.
  Reconnection depends on shutdown, a reconnect token and retry eligibility. Its
  comments explicitly allow either a still-running executor or an executor that died.
- [Non-retryable re-enqueue handling](https://github.com/buildbuddy-io/buildbuddy/blob/f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962/enterprise/server/scheduling/scheduler_server/scheduler_server.go#L3385-L3398)
  deletes the scheduler task and calls `MarkExecutionFailed` with an internal error.
- [Failure publication](https://github.com/buildbuddy-io/buildbuddy/blob/f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962/enterprise/server/remote_execution/execution_server/execution_server.go#L1467-L1494)
  publishes and retains a completed error operation. Such an operation need not
  contain a worker's `ActionResult` or completion metadata.

The matching status/message is consistent with this path. Hosted scheduler/lease
logs and server revision are unavailable, so the actual connection failure, worker
termination, retry history and reason for any late execution remain unproven.
Do not infer live or dead workers from disconnected BES, an indexed `EXECUTING`
row or zero timestamps. A retained exact result with completed worker metadata is
stronger evidence for that action than its stale execution index.

The earlier CLI observer-cancellation repair addresses a different failure: an
observer error canceling an accepted runner. It does not repair this scheduler
lease path. Neither upstream issue #13270 nor #16025 is established as the same bug
by this message alone.

## Recovery and next evidence

Keep the existing [`--disable_retry=true` remote-command policy](../docs/remote.md):
an observation failure must not automatically replay the whole script. Inspect the
outer and child invocation records, then retrieve retained results by exact
execution ID before deciding whether new work is needed. The explicit hosted rerun
above is counted as a new attempt; it is not evidence that the old worker stopped.

If this recurs, preserve the outer/child IDs, exact execution IDs, terminal
`ExecuteResponse`s and worker timestamps. Correlate those with provider lease logs
and deployed server revision before attributing the connection loss or proposing
a scheduler fix. Raw test logs and fixture artifacts remain private; this note
contains reviewed findings only.
