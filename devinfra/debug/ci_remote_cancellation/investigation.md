# Remote work after CI cancellation

**Status: open, 2026-10-28 UTC.** Explain why a VM test action associated with a
canceled CI job ran to completion, determine the remote producer's actual stop
latency, and establish whether that action still had a live consumer. No runtime
change is selected yet.

## What is established

- A VM test action was queued 8.64 seconds after the child cancellation request
  was acknowledged, then executed for 513.007 seconds and passed all three tests.
- This establishes continued action execution after acknowledgment. It does not
  establish that the remote Bazel build remained active beyond ten seconds,
  that Bazel sent its execution request after acknowledgment, or that all of the
  action's work was unnecessary.
- The [protocol probe](admission_fence_probe.md) deliberately kept Bazel running
  after canceling its inner invocation. It proved that `CancelExecutions` does
  not prevent later submissions. It did not reproduce a failure to stop the
  outer runner through the normal cancellation path.
- Ten seconds is the documented lease default, not a measured hosted setting.
- The [normal cancellation reproduction](normal_path_reproduction.md) now
  reproduces continued successful action execution: both a plain action and a
  controlled analysis-barrier case succeeded on their first attempt. New
  executions completed 30-second commands after the wrapper exited 130 and
  cancellation was observed. The barrier case establishes causal ordering;
  neither case measures outer process termination or attributes the requeue.
- The [retry-disabled comparison](retry_comparison.md) found no replacement in
  the plain case during 65 seconds with `--remote_retries=0`; a repeated default
  control requeued and completed the action. However, the barrier case still
  completed a new 30-second action with retries disabled. Retry suppression
  alone does not solve continued work after cancellation.
- A [client RPC trace](rpc_retry_trace.md) directly captured a child-only control
  receiving terminal action `CANCELLED`, then starting a fresh same-action
  `Execute` 105 ms after the original RPC closed. The replacement succeeded.
  This confirms the retry mechanism in that control. Two further normal-path
  probes reproduced the replacement but did not retain their trace artifacts.

## Cancellation ownership

```text
GitHub entry shell
  |-- TERM --> local bb CLI --> CancelExecutions(outer runner invocation)
  `-- helper ---------------> CancelExecutions(inner test/build invocations)

outer runner VM: CI script --> Bazel build activity
                                  `--> actions on other RBE workers
```

Source entry points: [signal wrapper](../../ci/bb_remote_log.sh),
[child cancellation helper](../../ci/cancel_bb_invocations.py), and
[remote test/build coordinator](../../ci/bazel_ci.py).
Outer-runner cancellation and child-action cancellation are concurrent. Stopping
processes on the runner VM cannot directly terminate processes on other workers.
The coordinator can also create its preassigned build invocation after its test
phase; that build ID may not exist when the helper calls cancellation.

## Incident: 2026-10-27 VM test

[GitHub job](https://github.com/agentydragon/ducktape/actions/runs/40574978735/job/122438855995),
[test invocation](https://app.buildbuddy.io/invocation/e5650420-cea1-5cc0-9029-b6cad3c77625),
[outer runner](https://app.buildbuddy.io/invocation/33509de5-4603-4149-ab15-8f8132fba13e).
Source: `4c32178e900aaea1126be5030c79373a0114eeca`.
Exact IDs, worker timestamps, cancellation log entries, and test-result digests
are retained in [continued_action.json](continued_action.json).

| Event                                             | UTC                | Relative to child cancellation acknowledgment |
| ------------------------------------------------- | ------------------ | --------------------------------------------: |
| Entry forwards cancellation to `bb`               | 20:28:34.7980764   |                                      -1.106 s |
| Build invocation cancellation request fails       | 20:28:35.7112732   |                                      -0.192 s |
| Test invocation cancellation request acknowledged | 20:28:35.9036531   |                                             0 |
| GitHub job completes as canceled                  | 20:28:40           |                                      +4.096 s |
| VM test action queued                             | 20:28:44.544168049 |                                      +8.641 s |
| Action command begins                             | 20:28:54.552237029 |                                     +18.649 s |
| Action command completes, exit 0                  | 20:37:27.559353994 |                                    +531.656 s |

The cached worker response gives 513.007 seconds of command execution. Its
`test.xml` records three tests, zero failures/errors/skips, and 478.454 seconds
of pytest time; the remaining action time is outside that pytest interval.
These are worker execution timestamps and test results, not UI update times.
The acknowledgment timestamp is when the GitHub helper logged HTTP success,
not the server's exact task-deletion time. Cross-host clock alignment is not
independently calibrated; these offsets are not a causal proof of submission order.

The saved execution index contains two `NEW` records for the same action digest:
one canceled with epoch-zero worker timestamps, and one with the successful
worker result above. Different execution IDs for the same digest are not enough
to identify a retry or hedge. `NEW` identifies an originating link; it does not
establish that no other invocation subsequently shared the execution.

Reverification on 2026-10-28 retrieved the GitHub cancellation log, cached worker
response, and test XML. A fresh `GetExecution` lookup returned an empty response;
link-type evidence therefore comes from the retained earlier index snapshot.
Do not substitute the index response for the cached execution result.

## Lease mechanism and observability

Inspected upstream source: `a04caa741548187c77dc991c8d8384c6788e24a9`.
The hosted server/executor revisions and exact CLI-source mapping are unknown.

1. The [cancellation service](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/execution_server/execution_server.go#L2123-L2190)
   enumerates current unfinished executions and skips shared actions. With no
   executions to cancel, it can return success without recording cancellation
   intent for future submissions.
2. [Scheduler cancellation](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/scheduling/scheduler_server/scheduler_server.go#L3162-L3164)
   deletes the task record. On a subsequent lease renewal, the scheduler finds
   the task missing and rejects renewal.
3. The [worker lease loop](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/scheduling/task_leaser/task_leaser.go#L195-L249)
   cancels its execution context after the lease error, driving command cleanup.
4. Independently, the server can publish a
   [synthetic terminal execution result](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/execution_server/execution_server.go#L1439-L1484).
   Terminal state and local CLI exit are not acknowledgment that remote build
   activity has stopped.

The [documented default](https://www.buildbuddy.io/docs/config-all-options/) for
`remote_execution.lease_duration` is `10s`. The authoritative runtime value is
[`LeaseTaskResponse.lease_duration_seconds`](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/proto/scheduler.proto#L81-L89),
which the worker uses for renewal. The checked invocation/execution APIs and
retained client logs do not expose it. Confirming the hosted setting requires a
worker lease response, operator logs, or confirmation from BuildBuddy.

A short lease delay could allow an action to be queued and then run much longer
on another worker. The observed +8.641-second queue time is consistent with that
possibility; it neither establishes the hosted lease value nor proves this cause.

The [traced control](rpc_retry_trace.md) started its retry after 105 ms, while
the replacement's worker queue timestamp was about eight seconds later. Do not
interpret the incident's queue offset as retry backoff or a direct measurement
of how long the producer remained active.

## Competing explanations

| Hypothesis                                                            | Evidence so far                                                                          | Observation that distinguishes it                                                                   |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| A. Outer build activity continues until lease cancellation arrives    | Queue time falls within the default lease interval; actual stop is unobserved            | Outer task-deletion time, actual lease response, renewal failure, and command-reap/build-stop event |
| B. Execution request was already in flight when cancellation occurred | Client request-send time is absent                                                       | Correlate client `Execute` send and server admission/queue events with cancellation                 |
| C. Retry or hedge creates another execution                           | Two execution IDs have the same action digest                                            | Scheduler dispatch reason, initiating invocation/attempt, and retry/hedge decision                  |
| D. Another live invocation still needs the action                     | `NEW` does not exclude later consumers                                                   | Complete execution-to-invocation links and each consumer's lifetime/cancellation state              |
| E. Cancellation only partially reaches its targets                    | Build-ID request failed; test-ID request acknowledged; outer acknowledgment time unknown | Outer and inner RPC outcomes, per-execution cancellation decisions, and lease loss                  |

The failed build-ID cancellation targets a different invocation; it does not by
itself explain the VM action linked to the test invocation.

The earlier [action merging/hedging investigation](action_merge_hedge_hang.md)
shows why execution identity and consumer ownership matter. Its different
incident and older source snapshot do not establish this incident's cause.

The [VM shutdown probe's CLI retry evidence](vm_shutdown_retry.md) records another
case where a transport retry reused the inner invocation ID. Keep its outer
attempts separate when interpreting execution indexes.

## Next evidence to collect

1. **Attribute the existing execution.** Correlate both execution IDs in the
   retained evidence with scheduler admission/retry/hedge decisions, all linked
   invocations, and the outer cancellation RPC. Retain an explicit access boundary
   if those facts require BuildBuddy's server logs. Resolve sharing before
   classifying the continued action as avoidable waste.
2. **Confirm the hosted lease.** Obtain the runtime lease response and the
   outer worker's task-deletion, lease-error, and command-stop timestamps. Record
   server/executor versions and configuration provenance. Inferring a lease from
   a terminal API record is insufficient.
3. **Extend the reproduced normal path with physical-stop evidence.** The
   [new probes](normal_path_reproduction.md) signal only the entry PID after an
   active execution is observed, capture outer/child cancellation, and verify
   continued worker execution using cached results and output. Still missing is
   positive evidence of outer command termination from outside the canceled
   process group. A missing heartbeat alone could be a transport failure; an
   idle reusable VM or Bazel server is not active build work. The
   [retry comparison](retry_comparison.md) supports retries for the plain requeue
   but leaves new submissions possible. Trace the initiating Execute/dispatch
   and physical outer stop to separate these mechanisms. Retain clock uncertainty.
4. **Exercise ownership cases separately.** For two owned consumers of one
   merged action, cancel one and verify the survivor receives its result. Record
   retries/hedges only if they actually occur. Use explicit barriers/releases
   and observed conditions; deadlines bound resources, not readiness waits.

For each probe record invocation, execution, action digest, worker, CLI attempt,
consumer links, signal/RPC events, lease events, and actual command-stop evidence.
Cancel only work created by that probe. Keep credentials, encoded commands, and
unfiltered API responses out of committed evidence.

## Retrieving the retained incident evidence

From the normal repository devshell with BuildBuddy/GitHub credentials:

```bash
bb execution get /uploads/35b6d1b3-e937-49c9-9d12-762a5c7f42b0/blobs/feee7670f67fe76b916e21396551bcd9cac9fd7ee513b114da49dc5ecb019b12/534 --output=json > continued-action-response.json
bb download e4e26d5681e873852c425ad0abdcc7c8bbbca95897da712da01f36d4e2bed10f/643 > test.xml
gh api repos/agentydragon/ducktape/actions/jobs/122438855995 > job.json
gh api --allow-escape-sequences repos/agentydragon/ducktape/actions/jobs/122438855995/logs > job.log
```

These commands were verified on 2026-10-28; remote artifact retention may later
limit retrieval. Keep raw responses private and extract only the fields needed
for the investigation, as in the committed evidence file.

## Closure criteria

Identify which request/dispatch path produced the continued action, whether it
had another live consumer, and when the outer build activity actually stopped.
Then choose and reproduce a change against that mechanism. A successful cancel
RPC or terminal record alone does not close the investigation; the general
absence of a future-submission fence alone does not prove a BuildBuddy defect
caused this incident.
