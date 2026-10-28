# Normal cancellation reproduced: 2026-10-28

**Reproduced in both attempted cases:** one plain action and one action held
behind an analysis barrier. Both used the existing signal wrapper and actual
`bb remote`, canceled the outer runner and child invocation, and exited locally
with code 130. New remote executions subsequently completed successfully.
These are two observations, not an established reproduction probability or a
claim of determinism.

[Selected evidence](normal_path_reproduction.json) retains execution identities,
worker timestamps, observer events, output digests, and downloaded output.
The [active investigation](investigation.md) tracks attribution and remaining
observability gaps.

## Plain action: no barrier or post-cancel submission by the controller

[Child](https://app.buildbuddy.io/invocation/73f2d9b7-9786-4efd-af51-d89fb6914c56),
[outer](https://app.buildbuddy.io/invocation/51d8e683-6c47-4cf8-8201-c158091ada1d).

The controller waited for `//:active` to be `EXECUTING`, then sent SIGTERM only
to the Bash entry PID. The wrapper forwarded TERM to the local CLI and ran its
child cancellation helper. The helper logged success; the entry exited 130;
the outer execution record was terminal with cancellation status code 1.
The original action also received a terminal cancellation record.

| Event                                                 | UTC                |
| ----------------------------------------------------- | ------------------ |
| Observer sends TERM to entry                          | 03:36:09.303911    |
| Observer reaps entry, exit 130                        | 03:36:09.517883    |
| Observer has checked child acknowledgment/outer state | 03:36:09.608652    |
| New execution queued, worker metadata                 | 03:36:17.719125002 |
| New action command starts                             | 03:36:23.775027726 |
| New action command completes, exit 0                  | 03:36:53.823766495 |

The new execution has the **same action digest** as the canceled original and a
different execution ID. Its command ran for **30.049 seconds**. Downloading its
output yielded `active-action-completed`. Relative to the observer's completed
cancellation checks, queue/start/end were approximately +8.110/+14.166/+44.215
seconds. Cross-host clocks were not independently calibrated.

The controller did not release a gate, restart the CLI, or submit another build
after cancellation. There was one local CLI launch and no transport-retry
message in the retained entry log. This does not exclude an internal Bazel retry,
server redispatch, or another source of the new execution.

## Controlled analysis barrier: causal ordering

[Child](https://app.buildbuddy.io/invocation/c59acdab-5166-4bb2-949f-7d4736b48376),
[outer](https://app.buildbuddy.io/invocation/73225d45-0454-4336-a9a0-b36a66acaabf).

The workspace had two independent top-level targets. `//:active` ran while a
repository rule supplying `//:late`'s input waited for a unique CAS marker.
After observing the active execution and barrier-entry marker, the controller
canceled through the same entry PID and verified exit 130, child cancellation
acknowledgment, and the outer terminal cancellation record. Only then did it
publish the release marker.

| Event                                       | UTC                |
| ------------------------------------------- | ------------------ |
| Observer sends TERM to entry                | 03:33:13.320902    |
| Observer reaps entry, exit 130              | 03:33:13.534798    |
| Observer completes cancellation checks      | 03:33:13.620288    |
| Observer finishes publishing release marker | 03:33:13.830189    |
| Late execution queued, worker metadata      | 03:33:14.071094956 |
| Late action command starts                  | 03:33:23.215780175 |
| Late action command completes, exit 0       | 03:33:53.273780435 |

The downloaded output includes the unique release digest and
`late-action-completed`. Its command ran for **30.058 seconds**. The input could
not be generated until the marker was published, so new action work became
runnable after cancellation without relying on clock alignment. The controller
published a cache blob, not another Execute request.

Another execution of the original active action also appeared. It was still
`EXECUTING` when the late action finished; the controller then requested cleanup
of both owned invocations. Its eventual physical stop was not measured.

## Reproduction procedure

Environment: source `aa9196b606ac8f6b60474d114f0fe3c8a00f0bc9`, pinned CLI
5.0.466, Bazel 9.2.0, outer runner properties/image from
[bbr.json](../../bbr.json), and action image
`ghcr.io/agentydragon/rbe-container-image@sha256:478978590586fa9688ed0b5d8b02c7c8534ed80824b72d27e1c51e9fe53df207`.
Hosted server/executor revisions and lease configuration are unknown.

The disposable controller and remote scripts are retained locally in
`/tmp/ci-normal-cancel-repro-2026-10-28/source-snapshot/`; raw evidence is in
adjacent per-child UUID directories. These paths are temporary experiment
artifacts, not a checked-in supported tool. The procedure below records the
essential conditions for rebuilding the probe.

1. Allocate a fresh child UUID and a standalone temporary Bazel workspace on an
   outer `bb remote` runner. Use the repository runner properties/image. Forward
   the existing BuildBuddy credential only to that trusted outer runner; its
   temporary bazelrc supplies remote/BES authentication. Never put it in an
   action environment, command, or input.
2. Define a single genrule with one output and this bounded command:

   ```sh
   timeout 30s tail -f /dev/null || test $$? -eq 124; echo active-action-completed > $@
   ```

   The 30 seconds is the simulated workload, not a readiness delay. Invoke
   `build //:active` with RBE/cache/BES configured, the action image above,
   `--noremote_accept_cached`, `--invocation_id=<child UUID>`, and
   `--action_env=CANCEL_PROBE_CHILD_ID=<child UUID>` to give each probe a unique
   action identity. Leave remote retry settings at their defaults.

3. Launch the actual CLI through the existing
   [Bash wrapper](../../ci/bb_remote_log.sh), with
   `INPUT_BAZEL_INVOCATION_IDS=<child UUID>`. Signal that Bash PID only; signaling
   a Python `bbr` parent or the whole process group changes the experiment.
4. Poll child `GetExecution` until an execution is `EXECUTING`. Record its ID.
   Resolve the owned outer invocation from child BES
   `buildMetadata.PARENT_INVOCATION_ID`, then send SIGTERM to the entry PID.
5. Check local exit 130, the helper's `cancellation requested for <child UUID>`
   log line, and the outer's terminal cancellation record. Continue observing
   the child from a process outside the canceled group. Track **all execution
   IDs**, including new IDs for the original digest. The first canceled record
   alone would incorrectly classify this experiment as successful cancellation.
6. Retrieve a successful execution's cached response and output, verifying its
   exit code, worker execution interval, and completion marker. Bound observation
   and workload duration. Finally call `CancelExecutions` for only the child's
   and resolved outer's owned UUIDs, including after an unsuccessful probe.

For the controlled variant, add a second root target `//:late` whose input is
generated by a repository rule in `@gate`. Build both root targets with
`--experimental_merged_skyframe_analysis_execution`; do not make the external
target itself a top-level pattern, which would block target-pattern loading.
The rule uploads `entered:<child UUID>` to CAS, then waits for a precomputed
digest of `released:<child UUID>`. Use `bb upload --stdin --digest=...` and
`bb download ... --output_file=...`, treating only NotFound as pending.
Require both the entry marker and active execution before signaling. Publish
the release payload only after the cancellation checks. Have the rule emit the
release digest into an input file, and copy it into the late action's output
before its bounded 30-second workload. The active workload in this variant was
bounded to 90 seconds. Credentials stay in repository evaluation on the trusted
outer runner, outside action inputs/environment.

## Interpretation and next discriminating test

This reproduces continued successful action execution through the normal
cancellation wrapper. It does not measure the outer process's physical stop,
the hosted lease duration, or complete consumer ownership. Both cleanup API
requests returned HTTP 200 in each case; that is an acknowledgment, not a worker
reap observation.

Bazel 9.2.0 provides a plausible mechanism: a non-OK execution response becomes
an [ExecutionStatusException](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/GrpcRemoteExecutor.java#L72-L94),
and its [remote retrier](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/RemoteRetrier.java#L45-L72)
treats `CANCELLED` as transient while the local action thread is not interrupted.
A child cancellation response can therefore arrive before outer cancellation
interrupts Bazel. This source path is compatible with the same-digest requeue;
the experiment does not establish that it produced it.

Next, contrast the plain probe with `--remote_retries=0` and capture the initiating
Execute/dispatch reason, while independently observing outer command termination.
A single contrasting run would be supporting evidence, not complete attribution.
Do not change CI retry policy on the strength of these two observations alone.
