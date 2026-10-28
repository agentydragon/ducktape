# RPC trace: Bazel resubmits a canceled action

**Directly observed in a child-only cancellation control:** Bazel received a
terminal `CANCELLED` action result, then started a fresh `Execute` for the same
action **105 ms after the first RPC closed**. The new execution completed
successfully. This confirms the retry mechanism in the control; it does not
capture the original CI incident or the full normal cancellation sequence.

[Selected RPC and worker evidence](rpc_retry_trace.json) retains both requests,
their terminal responses, timestamps, execution identities, and successful
output. The raw binary trace and API responses are retained privately.

## Captured sequence

[Child](https://app.buildbuddy.io/invocation/90320f94-9886-4d6c-8998-ee98aa210de0),
[outer](https://app.buildbuddy.io/invocation/5c2bf3a3-8200-42a9-9c3c-99b2b6cdfe76).

The control used the same standalone plain-action workspace, RBE image, CLI
5.0.466, Bazel 9.2.0, and default remote retries as the
[earlier comparison](retry_comparison.md). After the original action reached the
`EXECUTING` index stage, the controller called `CancelExecutions(child)`. It
deliberately left the outer build running so Bazel could close and publish the
RPC log. The entry eventually exited 0. This is a mechanism control, not the
normal entry-PID signal experiment.

| Event                                                  | UTC                |
| ------------------------------------------------------ | ------------------ |
| Original `Execute` starts, client RPC log              | 04:07:19.114       |
| Observer starts child-only cancellation request        | 04:07:24.585245    |
| Original `Execute` closes, terminal action `CANCELLED` | 04:07:24.666       |
| Observer receives cancellation HTTP 200                | 04:07:24.683058    |
| Fresh `Execute` starts, same action digest             | 04:07:24.771       |
| Replacement queued, worker metadata                    | 04:07:32.921143834 |
| Replacement command starts                             | 04:07:33.019668839 |
| Replacement command completes, exit 0                  | 04:08:03.060809882 |
| Second `Execute` closes successfully                   | 04:08:03.141       |

Both calls used the same child invocation and action digest
`e628e36c8a053fc1229e902f03e322f1bbdd994a7603e6b40c594390b9e657f9/310`.
The original execution's upload UUID was
`9774338f-858a-4694-993e-441e9125e69c`; the replacement's was
`c5bd4796-b1c5-40ef-99bf-d2ba27622417`. The trace has two `Execute` calls and no
`WaitExecution` call. Both requests set `skip_cache_lookup=true`.

The distinction between the two status fields matters:

- The first RPC's **transport status was `OK` (0)**. Its final operation had
  `done=true` and an **`ExecuteResponse.status` of `CANCELLED` (1)**.
- The second RPC's transport status was also `OK`. Its final operation had
  `done=true`, action status `OK`, and action exit code 0. Downloaded output was
  `active-action-completed`; its command ran for 30.041 seconds.

This was a new execution request following a canceled action result, rather than
reconnecting to watch the existing operation. The 105 ms interval uses two
timestamps from the same client log, at millisecond resolution. Observer and
worker clocks were not independently calibrated.

## Source path matching the trace

In Bazel 9.2.0,
[GrpcRemoteExecutor](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/GrpcRemoteExecutor.java#L175-L229)
sets its wait state from whether the received operation is done, then processes
the result. A non-OK action status becomes an `ExecutionStatusException`.
For a terminal result, the next attempt uses `Execute`, not `WaitExecution`.

The [configured retry classifier](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/RemoteRetrier.java#L40-L79)
treats `CANCELLED` as transient if the local Java thread is not interrupted.
Its initial backoff is 100 ms with 10% jitter, consistent with the measured
105 ms interval. It does not check the invocation's cancellation state.

The replacement's worker queue timestamp was approximately eight seconds after
the fresh RPC began. **The earlier approximately eight-second queue delays are
not measurements of retry backoff or of outer Bazel's survival time.** RPC-start
time is also not an exact network-send or server-admission timestamp. Those
boundaries remain untraced.

## Capture method and normal-path attempts

Add `--remote_grpc_log=$BUILDBUDDY_ARTIFACTS_DIRECTORY/remote_rpc.bin` to the
child Bazel command. Keep its authentication bazelrc outside that directory.
After normal build completion, the runner publishes the file through the outer
invocation's `NamedSetOfFiles` BES event. Query the outer invocation directly;
automatic workflow-to-child resolution can miss this outer step artifact.

The captured file was 8,496 bytes, with SHA-256
`25fde1b17cdba920bd723927535ffe04a651533845ae8786967d614bf230210c`.
It contained eight complete length-delimited
[LogEntry messages](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/protobuf/remote_execution_log.proto)
and no incomplete tail. Only the two execution RPCs' selected fields are
committed. Their operation IDs match the execution index, and the replacement's
output digest matches its downloaded output.

Two attempts used the normal signal wrapper, canceled outer and child, and
exited locally with 130. Both again produced successful replacement actions:

- Child `1e249ddc-4afd-4aa5-b088-047c4ad88f4c`, outer
  `cad03378-2b3c-4329-95c8-ca3d767d9750`: logging enabled without another
  intervention; no trace artifact was found in outer BES.
- Child `4edb5e0a-da07-4534-8324-6b0759c18ed2`, outer
  `3473cd6c-0f9a-46c8-a957-ce2b3c21071e`: after observing the replacement ID,
  the controller published a unique CAS marker asking the remote wrapper to
  interrupt its Bazel client and flush the log. This happened after the requeue
  and cannot explain that requeue. No trace artifact was found; consumption of
  the marker and the extra interrupt were not confirmed. The cached outer
  response contained cancellation status with no ActionResult or stdout digest.

Bazel's [logging interceptor](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/logging/LoggingInterceptor.java#L118-L150)
writes each record when the RPC closes, and its
[output stream](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/util/io/AsynchronousMessageOutputStream.java#L47-L77)
has a 100 KB buffer. Runner artifact publication happens after the command.
These are capture hazards under cancellation; the missing artifacts do not
measure the producer's physical stop or prove which hazard lost the log.

Raw evidence and source snapshots are under
`/tmp/ci-cancel-rpc-trace-2026-10-28/` and the adjacent earlier per-child probe
directories. Sources used commit `215af6378b9ddf23c12ce3d22f1a761f782454e7`
plus the disposable instrumentation. All six final cancellation requests for
the exact owned child/outer IDs returned HTTP 200. The control's requests were
after its entry completed; no unrelated invocations were canceled.

The remaining attribution gap is a trace retained independently of the canceled
outer runner, or BuildBuddy server tracing correlating the normal-path
cancellation, client request, and new execution. Physical outer command-stop,
lease duration, and consumer-sharing evidence remain separate gaps.
