# Hosted worker trace request

**Status: drafted, not sent; 2026-10-28 UTC.** The
[producer-stop probe](producer_stop_witness.md) confirmed command completion in
one single-action case. Its blocked-analysis case retained no positive shutdown
receipt. This packet requests the host-side events needed to resolve that case.

## Access checked

A fresh `GetExecution` lookup with `inline_execute_response=true` for both the
blocked child and its outer runner still returned status `CANCELLED`, no worker
ActionResult, and epoch-zero worker timestamps. It therefore supplies no physical
stop observation. [Selected API evidence](host_trace_access.json) records the
exact requests, results, collection times, and hashes of the private responses.

`GetExecutionNodes` returned HTTP 403, `PermissionDenied`, for the current API
key. That prevents executor metadata enumeration with this key. Even a successful
node listing would provide registration/configuration metadata rather than a
per-task process-exit or lease trace. The inspected execution APIs expose stage
and resource timings, not the requested lease-renewal and VMM-stop events:
[execution API schema](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/proto/execution_stats.proto#L299-L324),
[node metadata schema](https://github.com/buildbuddy-io/buildbuddy/blob/a04caa741548187c77dc991c8d8384c6788e24a9/proto/scheduler.proto#L669-L690).

The repository's self-hosted executor manifest is
[parked with zero replicas](../../../cluster/parked/buildbuddy-executor/helmrelease.yaml).
That is repository configuration, not a fresh cluster-state observation, and
does not provide access to the hosted worker. No key expansion or infrastructure
change was attempted. The next evidence requires a hosted operator or an
operator-provided trace export.

## Related Bazel fix to probe separately

Bazel [issue #30954](https://github.com/bazelbuild/bazel/issues/30954) reports a
server continuing its command after its client is killed. The upstream fix,
[29b6ec37](https://github.com/bazelbuild/bazel/commit/29b6ec37e9aff603d4da51570ffb175071605de1),
was committed on October 1. It makes client disconnection interrupt the command
thread once, rather than relying on later output writes and potentially
interrupting a progress-reporting thread. This is related source behavior,
not an attribution of our unobserved hosted shutdown.

On October 28, the latest release API reported 9.3.1. A direct source check
found the old cancellation handler in both 9.2.0 and 9.3.1; the fix commit has
the new handler. The [selected evidence](host_trace_access.json) records source
hashes and refs. Upgrading to 9.3.1 alone therefore does not include this fix.
A bounded probe with a build containing that change could test its relevance;
no Bazel pin or binary was changed in this investigation. Keep the hosted
trace request open because this source fix does not establish when the hosted
outer worker stops, or how long its lease lasts.

## Draft request

Please correlate this owned cancellation probe on the hosted service. We need
the worker's physical cancellation sequence, separately from the server's
synthetic terminal cancellation result.

- **UTC window:** `2026-10-28T05:03:40Z` through `2026-10-28T05:05:10Z`.
- **Outer runner invocation:**
  [96c2cba2-507f-4c5e-9231-8b19602d337b](https://app.buildbuddy.io/invocation/96c2cba2-507f-4c5e-9231-8b19602d337b).
- **Child Bazel invocation:**
  [ac648165-ec92-4138-943f-17c49026f596](https://app.buildbuddy.io/invocation/ac648165-ec92-4138-943f-17c49026f596).
- **Outer execution:**
  `bb-snapshot/uploads/44f97f92-f798-4029-ba2c-35a80f52b413/blobs/blake3/7cc68722c4c9f597251afead78f3734884d74254561ab476b6464e5ced290cd4/149`.
- **Initial child execution:**
  `/uploads/6a19728a-2528-4627-8ee0-4454d12ae906/blobs/47290b996b9bad63cef5e0eeac414f53e3ab2c42ff4636715440fb91622f13c6/310`.
- **Guest Bazel identities:** client PID `27726`, start ticks `13608454`;
  Java server PID `27743`, start ticks `13608460`, PPID `1`. These are guest
  namespace identities; please correlate them with the actual worker/VM identity.
- **Client versions:** Bazel 9.2.0, CLI 5.0.466. Effective child remote retries
  were zero; the build used `--keep_going` and `--nopreemptible`. A repository
  rule held an independent target behind a unique CAS release marker.

| Controller observation                                              | UTC             |
| ------------------------------------------------------------------- | --------------- |
| Active child execution and detached-watcher readiness verified      | 05:03:48.556360 |
| TERM sent only to the local Bash entry PID                          | 05:03:48.556432 |
| Entry reaped, exit 130                                              | 05:03:49.170968 |
| Child cancellation acknowledgment and terminal outer record checked | 05:03:49.256781 |
| Final negative lookup for client-exit receipt saved                 | 05:04:44.139656 |
| Final negative lookup for same-server idle receipt saved            | 05:04:44.442198 |
| Final cleanup cancellation for child acknowledged                   | 05:04:45.160575 |
| Final cleanup cancellation for outer acknowledged                   | 05:04:45.238472 |

The wrapper concurrently forwarded TERM to the local CLI, which cancels the
outer invocation, and called `CancelExecutions` for the child. The remote
watcher had double-forked to guest PID 1 and opened exact client/server pidfds
before cancellation. It published readiness to CAS, independently of final
runner artifacts. No later exit, idle, or observer-error receipt was retained.
The analysis gate was **never released** in this attempt. No later-action
success is claimed for it. Controller and worker clocks were not calibrated.

Please provide a correlated timeline, including:

1. Outer and child cancellation receipt, task selection, task deletion, and
   synthetic terminal publication. Distinguish the initial request from the
   final cleanup requests approximately 56 seconds later.
2. The actual outer-task `LeaseTaskResponse.lease_duration_seconds`, next
   renewal/rejection, and worker execution-context cancellation. The documented
   ten-second default is not a measured hosted setting.
3. VMExec stream cancellation, descendant enumeration/kill, original Bazel
   client exit, Java server build-command cancellation or command-lock release,
   and any worker-side wait/reap evidence.
4. Output collection/finalization, runner disposal, and actual VMM process exit.
   State whether the detached watcher lost its reporting opportunity because
   of VM teardown or another cause.
5. Deployed server/executor versions and relevant cancellation configuration.
   If any later child submission occurred, correlate its client request,
   admission, execution identity, and live consumers separately.

The hypothesis is that outer teardown can remove the in-guest witness before
it reports, but that is unproven. We need positive host-side stop events to
separate it from continued producer work. A successful cancellation RPC or
terminal UI state alone cannot resolve the question.

For comparison, the single-action zero-retry case
[9ab90295](https://app.buildbuddy.io/invocation/9ab90295-86a2-4d8f-8666-8165ac3e3923)
retained a client-exit receipt and a successful info command on the same original
Java server. Its verified receipt arrived within 2.302 seconds of the entry
signal; see the [retained evidence](producer_stop_witness.json). That result
shows the channel can work, but does not establish its reliability during VM
teardown.

Only sanitized identities and selected evidence accompany this draft. Raw
invocation responses and encoded runner commands remain private because they
can contain credentials. No request to BuildBuddy has been sent.
