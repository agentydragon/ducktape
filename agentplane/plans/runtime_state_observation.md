# Runtime state observation and Gateway ownership

## Selected contract

The selected Runtime/Gateway contract is recorded in the
[Runner Gateway README](../runner_gateway/README.md#replica-ownership-decision); this plan tracks its
implementation status and deployment gates.

The leader-elected Sandbox Controller observes the current Runtime directly and keeps facts in
memory keyed by Sandbox UID, workload incarnation and boot ID. For a VM, the incarnation is the
virt-launcher Pod UID. On leadership acquisition or stream replacement, it relists current workloads
and waits for an authenticated snapshot matching that identity before using facts or starting an idle
timer. Callback generation and identity must still be current when facts or timers are updated.

The authenticated Controller `WatchState(observe_only=true)` is the source that binds the workload
incarnation in a Runtime process. The Runtime listener is available before this watch arrives, and
each Runtime process creates its own fresh boot ID. The launch file contains the Sandbox identity,
not the workload Pod UID, so Runtime has no shared local source for that UID across Pod, VM and
Firecracker modes. The Controller's exact SVID and current workload observation provide that
authority. Runtime reserves the UID before invoking ordinary lifecycle adoption, then permits
same-UID retries after stream loss or an uncertain adoption result; it never changes the binding
within that process. For a replacement process, the Controller supplies the replacement workload
UID, and Runtime restores retained state through its normal lifecycle barriers.

Any authenticated Runner Gateway replica may call a Runtime once it has the Controller-bound
Sandbox UID, incarnation UID and boot ID. Calls authorize against that binding independently of
Gateway state-watch lifetime. A Gateway `WatchState` may observe only the already-bound UID; no
Gateway watch can establish or advance the Runtime's incarnation. Gateway SVID authentication
establishes service identity; Gateway still enforces caller and per-method authorization. Dispatcher
delivery Leases remain a separate mechanism and are out of scope.

The Controller uses a direct passive Runtime state stream with its own narrow SVID; it does not
install Gateway call ownership. `Quiesce` continues through the Gateway. Runtime restarts, workload
replacement and same-name Sandbox replacement invalidate old calls and facts through the boot ID,
incarnation UID and Sandbox UID respectively. Another authenticated Gateway can call the current
Runtime after its own observation succeeds, but stale callers cannot.

The current workloads preserve the UID binding through their normal lifecycles: Firecracker RAM
restore uses the same launcher Pod; KubeVirt stop/start creates a new VMI, launcher Pod and Runtime
boot. KubeVirt live migration that replaces the launcher Pod while preserving the Runtime has not
been implemented or tested here. Supporting that case would require a separate trusted migration
authority to transfer the binding; a new Gateway watch is not such an authority.

## Loss, lifecycle and outcomes

Use the current 10-second snapshot heartbeat and 30-second observation timeout as the initial
freshness window. On stream loss, retain the last same-incarnation snapshot as stale for status,
retry after one second, and reset the idle timer. Stale facts do not prove connectivity, quiescence or
idleness; after the grace window report the Runtime disconnected. A fresh snapshot for the current
boot restores observation and begins a new idle interval.

Persisted `QuiesceAttempt` and its retry deadline survive leadership change and stream loss. Do not
clear it, stop the Pod or release its state volume from a stale `quiesced` fact. Keep terminal
bootstrap outcomes terminal for their incarnation; never rerun a succeeded, failed or interrupted
bootstrap because observation was lost. A replacement incarnation starts its own bootstrap state.

Gateway forwards each execution mutation at most once. If a call ends after Runtime may have acted,
the outcome is uncertain and failover, Runtime restart or reconnect never triggers automatic replay.
Dispatcher queue and command-delivery behavior remains owned by its separate delivery Lease.

## Deployment and cleanup

The independent-observer Gateway route and stable-Service callers are implemented in merged #16365
(`efacbf509178221d60e392168ba812aff360beae`). No Gateway connection Lease or per-Sandbox control
Service/EndpointSlice creation remains. Neither service reconciles or deletes retained route objects.
Objects with Sandbox owner references are collected only when that Sandbox is deleted; unowned
objects require manual deletion. Ordinary suspension and Pod replacement collect neither.

The final RBAC and protocol cleanup is gated on staging image pins. Before merging that cleanup,
verify both that the Runner Gateway image containing #16365 is pinned and running in staging and that
the Sandbox Controller image containing #16365 is pinned and running there. The Gateway pin proves
old replicas no longer publish connection Leases or EndpointSlices. The Controller pin is separately
required before removing Service permissions because an older Controller could still create or watch
per-Sandbox Services. Do not treat a template pin as proof that an existing Pod or VM uses the image.

The cleanup removes Gateway connection-Lease and EndpointSlice permissions while preserving
Dispatcher delivery Lease permissions; it removes the Controller's per-Sandbox Service permissions
while preserving its leader-election Lease permission. It also removes Runtime's connection-epoch
protocol fields and receiver tolerance. Runtime-to-Gateway connections are not part of this route.

The separate strict-settings PR removes `proxy_port` acceptance and the Pod-IP environment input.
It merges only after the preceding cleanup's configuration without `proxy_port` has applied and its
Gateway image, which no longer reads Pod IP, is pinned and running on staging. Removing the decoder
before the configuration rolls would fail strict startup; removing Pod IP before the accepting image
rolls would fail old-image startup.

## Acceptance

Acceptance must cover authenticated Controller-only initial binding, same-UID reconnect, refusal to
change UID after adoption begins or an uncertain callback, stale callbacks/snapshots, idle reset,
Sandbox/incarnation/boot fencing, and independent Gateway watches against the bound UID. It must also
verify watch loss does not cancel Runtime-owned admitted work and uncertain mutations are not replayed.
Deterministic two-replica `gateway_test` coverage exercises failover, Runtime restart, same-name
replacement and independent Dispatcher delivery. Staging image pins remain the deployment-specific
gate above.
