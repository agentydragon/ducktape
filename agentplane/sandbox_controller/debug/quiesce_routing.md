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
unauthorized callers, a replaced incarnation and an unready Pod remain rejected. An added
`SubmitInput` assertion on the same held snapshot passed in
[invocation `1880ea00`](https://app.buildbuddy.io/invocation/1880ea00-4ced-4d5b-9a95-6d5c89b8fd5c).

One uncached real Pod storage case on commit `0c5aa3e3b4` passed in
[invocation `eb6c4ddd`](https://app.buildbuddy.io/invocation/eb6c4ddd-87a8-4db9-a73b-12a65e689793).
The storage artifact reports `passed`, `cleanup_complete`, and `incompatible_claim_refused`.
The old Runner's first controller Quiesce at 10:22:22.824–22.837 UTC and replacement Runner's
first at 10:22:35.670–35.679 both recorded gRPC status 0, with no one-minute retry.
The replacement call has linked controller, Gateway and Runner server spans in trace
`97a00da2c7a14f616d6c087d24563901`; the Runner server span ran at
10:22:35.675–35.678. These facts establish successful first-call forwarding in this sample.
The trace does not establish whether this sample passed through the held-suspend cache window,
nor that every possible source of `UNAVAILABLE` has been eliminated. Retained artifacts:
`process-spans.jsonl` SHA-256 `3d886b5713264bbc1c03cf0566d7b8320c503bd97e36d2cee31eafe8ffadb17d`,
`storage-pod-directory.json` SHA-256 `53d0a708d9ae941d26efd97f97dd56a4178360ee9792734fea2e4f1983029604`.
