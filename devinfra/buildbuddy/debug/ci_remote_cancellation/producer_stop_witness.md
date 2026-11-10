# Positive producer-stop witness

**2026-10-28 UTC:** a normal single-action cancellation with
`--remote_retries=0` now has positive evidence that its Bazel build command
finished. The launched Bazel command process exited; a subsequent `info server_pid` completed
on the **same original Java server**, with its PID and process start time
unchanged. The controller received that receipt **within 2.302 seconds of signaling
the entry shell**. That is an upper bound including publication and polling,
not an exact shutdown latency or a distribution.

The blocked-analysis case remains unobserved: its watcher was ready before
cancellation, but no exit or idle receipt arrived during the approximately
55-second observation window. Missing receipts do not establish continued work.

[Selected evidence](producer_stop_witness.json) includes process identities,
controller and retained remote events, receipt digests, source hashes, and
artifact digests. The [main investigation](investigation.md) remains open.

## Results

| Case                                                                   | Child / outer invocation                                                                                                                                                      | Observation                                                                                                                                                        |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Channel calibration: deliberately kill the launched command and server | [ec425559](https://app.buildbuddy.io/invocation/ec425559-57f6-4b3f-984c-866073e91af0) / [c408503a](https://app.buildbuddy.io/invocation/c408503a-2dca-4269-9134-188227ccc721) | Exact launched-command and server pidfds became readable; both CAS exit receipts survived. This is a calibration, not normal cancellation.                         |
| Normal wrapper, single action, zero retries                            | [dde2a084](https://app.buildbuddy.io/invocation/dde2a084-ad16-4f06-9b5a-c220c2578a8e) / [9ab90295](https://app.buildbuddy.io/invocation/9ab90295-86a2-4d8f-8666-8165ac3e3923) | Local exit 130; launched command exit 34 after `Cancelled: invocation cancelled`; same-server info succeeded.                                                      |
| Normal wrapper, blocked analysis, zero retries                         | [ac648165](https://app.buildbuddy.io/invocation/ac648165-ec92-4138-943f-17c49026f596) / [96c2cba2](https://app.buildbuddy.io/invocation/96c2cba2-507f-4c5e-9231-8b19602d337b) | Local exit 130 and terminal cancellation records; no positive stop receipt. The release marker was withheld, so this run did not test a later action's completion. |

An earlier control, child `eae39ea8-9fb9-4bb3-a633-91aef6bbbff4`, tried a single
SIGINT while repository evaluation was blocked. Its 30-second client wait
expired; the controller also timed out downloading a missing CAS marker.
Cleanup followed. That attempt is retained but excluded from successful channel
calibration. Later attempts checked CAS metadata before downloading markers.

All children reported effective `remote_retries=0` in BES. Standalone probe
commands selected that flag explicitly. Source was
`a545ff9a7fa99efc1d78c015e4fb750c701687d0`, Bazel 9.2.0, CLI 5.0.466, and the
runner/action images used by the [earlier reproductions](normal_path_reproduction.md).
The hosted server version and lease remain unknown.

## Why this is stronger than a cancellation record

The independent watcher double-forks, creates a new session, and verifies that
its parent is guest PID 1 before announcing readiness. It opens pidfds for the
exact launched-command and server processes before cancellation. Its stdout is detached from
the canceled command, and it publishes small, unique CAS receipts while running.
This does not depend on end-of-command artifact publication.

The `client` PID in this first probe came from `Popen` and can identify a
launcher. Its pidfd establishes that process's exit, not native-client identity
or Java build completion. The [later comparison](bazel_client_disconnect_comparison.md)
verifies the native executable separately.
For that second question, the watcher runs a read-only command against the same
output base. In Bazel 9.2.0:

- Every recognized command takes the dispatcher's exclusive command lock, which
  is released after the command returns. See
  [BlazeCommandDispatcher](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/runtime/BlazeCommandDispatcher.java#L197-L238)
  and its
  [release](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/runtime/BlazeCommandDispatcher.java#L301-L305).
- The Run RPC maps `block_for_lock` to waiting for that lock. See
  [GrpcServerImpl](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/server/GrpcServerImpl.java#L576-L634).
- `info server_pid` returns the current Java process PID. See
  [ServerPidInfoItem](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/runtime/commands/info/ServerPidInfoItem.java#L22-L32).

The probe pins `--nopreemptible` for the original build and uses
`--block_for_lock` for the confirming command. A nonblocking peek runs first;
it also succeeded in the single-action run. The final check requires exit 0,
the original server PID in stdout, an unreadable original server pidfd, and an
unchanged `/proc/<pid>/stat` start time. This rejects replacement servers and
avoids interpreting an idle reusable Java process as an active build.

### Retained single-action timeline

| Event                                                 | UTC             |
| ----------------------------------------------------- | --------------- |
| Controller signals only entry shell                   | 05:07:59.155657 |
| Local wrapper exits 130                               | 05:07:59.421014 |
| Watcher observes launched-command pidfd readable      | 05:08:00.009543 |
| Same-server blocking info returns, PID 3990 unchanged | 05:08:00.841398 |
| Watcher starts its own cleanup shutdown               | 05:08:01.451466 |
| Controller receives and verifies idle CAS receipt     | 05:08:01.456801 |
| Controller starts final cleanup cancellation requests | 05:08:02.377845 |

The same-server check and receipt publication precede the watcher's cleanup
shutdown. Receipt retrieval finishes slightly later. Remote event ordering is
also retained on one remote monotonic clock; the 2.302-second bound uses only
the controller's monotonic clock. Cross-host clocks are not calibrated.

The retained `watcher-events.jsonl` digest is
`71a2e2c6367f0833f677858d007bc9532e0841c29f82e0423b8d4ae7a6092d43/1171`.
The confirming stdout is `3990\n`, digest
`0b3d1512e2179373f7d8bcd4538fbf5cd223af8be6af0e570498cd5161c7f658/5`.
The CAS idle receipt is
`c2fcb3d9a79060a6020605edc735f38d903f1f91fa905ebaab99473117bfc145/70`.

## Rebuild the probe

The disposable scripts and raw API evidence are retained privately under
`/tmp/ci-producer-stop-2026-10-28/`, with per-attempt `source-snapshot/` copies.
These are experiment artifacts, not a supported checked-in runner.

1. Start the [standalone reproduction workspace](normal_path_reproduction.md#reproduction-procedure)
   on the normal outer runner. Give each attempt a unique child UUID and output
   base. Set `--remote_retries=0`, `--nopreemptible`, and an explicit bounded
   workload. Keep credentials on the trusted outer runner, outside action inputs.
2. Record the launched-command PID and `${output_base}/server/server.pid.txt`, including
   both process start times. Double-fork the independent watcher, verify PPID 1,
   and open both pidfds. Close inherited stdout/stderr and use a directory
   outside the runner workspace so the watcher does not pin that mount.
3. Publish `producer-stop-v1:<child UUID>:watcher_ready` to CAS. Only proceed once
   the external controller has verified that receipt and observed an executing
   action; require the repository-entry marker in the gated variant too.
4. Calibrate the channel separately by deliberately killing only the two owned
   Bazel PIDs and checking their exit receipts. For normal cancellation, signal
   only the existing Bash entry PID. Do not add a server kill or direct Bazel
   interrupt to this normal path.
5. When the watcher observes client exit, query the original server as above.
   Publish `client_exited`, `server_exited`, or `same_server_idle` receipts only
   when their corresponding predicate succeeds. The payload format is
   `producer-stop-v1:<child UUID>:<phase>`; its SHA-256 and byte count are known
   in advance. Check CAS metadata, then download and verify bytes. Non-NotFound
   errors are failures, not evidence of absent receipts.
6. In the gated case, release the repository barrier only after a positive
   busy-command receipt if testing continued work. No such receipt arrived in
   this attempt, so the controller never released it. Readiness uses observed
   conditions; deadlines bound the probe and simulated workload.
7. Retain any watcher artifacts and exact source hashes. In a successful outer
   command, wait for the detached watcher to exit before completion. Finally
   cancel only the owned child/outer UUIDs. All eight final cleanup API requests
   in these four attempts returned HTTP 200; this is not physical teardown proof.

## Remaining visibility boundary and sequencing implication

BuildBuddy source snapshot `a04caa741548187c77dc991c8d8384c6788e24a9`
explains why the watcher can sometimes report and sometimes disappear:

- Unix cancellation cleanup walks PPID descendants, stopping and killing them.
  Reparenting to PID 1 escapes that traversal; `setsid` alone does not. See
  [commandutil](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/commandutil/commandutil_unix.go#L89-L128).
- The Firecracker execution finalizer syncs/copies outputs before teardown. See
  [output capture](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L2874-L2927).
- Failed tasks are not recycled; removing the Firecracker container stops the
  VMM, which also destroys the guest watcher. See
  [recycling](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/runner/runner.go#L1693-L1705)
  and [VMM stop](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L3205-L3227).

The single-action result closes one observational gap: its producer finished
and its surviving server became idle. It does not prove physical termination of
remote actions, VM destruction, the hosted lease duration, or general behavior
while other work remains runnable. The gated result cannot distinguish process
survival from a lost observer or VM teardown. It also does not overturn the
earlier [zero-retry barrier reproduction](retry_comparison.md).

A production sequence of “stop producer, confirm, cancel remaining children”
therefore still needs a reliable stop channel and acknowledgment covering the
blocked case. The passive witness is not that implementation. Do not replace
that missing guarantee with a fixed delay or a successful cancellation record.

The [hosted trace request](host_trace_request.md) records a fresh API check and
the unsent operator handoff. For hosted-side follow-up, use the gated child/outer
pair above and request:
outer task-deletion time, actual lease response, lease-renewal failure, guest
command cleanup, Bazel client/server exit or command-lock release, and VMM-stop
timestamps, with deployed versions. A host-side event can survive destruction
of the guest witness. No request to BuildBuddy has been sent.
