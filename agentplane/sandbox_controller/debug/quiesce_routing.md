# Quiesce routing through a held suspend, 2026-10-31

The Pod storage case in [invocation `dd7ffc77`](https://app.buildbuddy.io/invocation/dd7ffc77-ecaf-4ef1-a021-e47849c292a6)
passed, but its replacement Runner's first Quiesce at 10:04:49.042–49.047 UTC returned gRPC
`UNAVAILABLE` (14). The controller and Gateway server spans share one trace; no Runner server span
appears for that call. After the persisted one-minute retry deadline, the next call at
10:05:49.035–49.061 reached the Runner and returned OK, then the Sandbox quiesced. The earlier
[Pod storage trace note](../../system_tests/debug/pod_storage_quiesce_stages.md) records the
same pattern in a prior run. The trace proves the first call failed before a retained Runner
server span, but does not distinguish Gateway destination rejection from connection-holder or
Runner transport failure by itself.

The source has a concrete rejection window. While bootstrap or Session setup runs,
`holdSuspend` returns `SuspendPending` with no stop stage; `updateStatus` persists
`operatingMode: Suspended`, an incarnation, and an empty `stopStage`. The Gateway classifies
that snapshot as `Suspending`, which `sandboxResolver.destination` rejects even for Quiesce.
When the hold ends, `Reconciler.quiesce` previously persisted only `quiesceAttempt` before
calling the Gateway; the old `Suspending` status remained durable until the outer reconcile
returned. Even after publishing a coherent `Quiescing` status with the attempt, the Gateway
informer can still serve its prior `Suspending` snapshot immediately after the write.

The routing fix has two parts: persist the desired operating mode, suspension reason, and
`Quiescing` stage atomically with the attempt, and permit only the controller-authorized
Quiesce RPC through a held `Suspending` snapshot. The Gateway still checks the Sandbox UID,
incarnation, Pod ownership/readiness, elected Runner connection and epoch. Other Runner proxy
methods remain unavailable in that phase, and the controller still waits for bootstrap and
setup before it calls Quiesce. No retry deadline or sealing barrier changes.

Focused controller and Gateway package tests passed in
[invocation `3eaa7a93`](https://app.buildbuddy.io/invocation/3eaa7a93-7c69-4336-986a-5ad4db0aaeb1):
the controller test reads the persisted status at the Quiesce call after a held bootstrap,
and the Gateway test uses the held snapshot to verify Quiesce forwarding while GetStatus,
unauthorized callers, a replaced incarnation and an unready Pod remain rejected. A real
post-fix Pod case is still needed to observe the first-call result; one passing sample would
not establish that every possible source of `UNAVAILABLE` has been eliminated.
