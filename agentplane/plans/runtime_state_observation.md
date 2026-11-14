# Runtime state observation and Gateway ownership

## Selected contract; Gateway cutover pending

The selected Runtime/Gateway contract is recorded in the
[Runner Gateway README](../runner_gateway/README.md#replica-ownership-decision); this plan tracks its
rollout and rollback.

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
Sandbox UID, incarnation UID and boot ID. Calls with an absent or zero connection epoch authorize
against that binding independently of Gateway state-watch lifetime. Nonzero call epochs retain the
legacy live-watch checks during compatibility. A Gateway `WatchState` without an epoch or with zero
may observe only the already-bound UID; legacy nonzero Gateway watches are also limited to that UID.
No Gateway watch may establish or advance the Runtime's incarnation. Gateway SVID authentication
establishes service identity; Gateway still enforces caller and per-method authorization. Dispatcher
delivery Leases remain a separate mechanism and are out of scope.

The Controller uses a direct passive Runtime state stream with its own narrow SVID; it does not
install Gateway call ownership. `Quiesce` continues through the Gateway. Runtime restarts, workload
replacement and same-name Sandbox replacement invalidate old calls and facts through the boot ID,
incarnation UID and Sandbox UID respectively. Another authenticated Gateway can call the current
Runtime without relying on a connection-epoch watch, but stale callers cannot.

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

## Migration and rollback

1. **Controller readers.** The direct-reader migration merged in #16324. Keep Lease fact publication
   and the shared codec until #16328 removes them after the new Controller is running on staging.
   The direct-observer Controller image is already pinned. The receiver behavior described above is
   introduced in this change without manifest settings or authorization allowlist changes.
2. **Gateway removal.** After this receiver change lands, pin the Runtime image containing it before
   routing callers through the stable `runner-gateway` Service. Image pinning does not replace a
   Runtime already running in a Sandbox: before the zero-epoch Gateway cutover, verify the live
   Runtime inventory and cycle older Sandboxes through suspend/resume so they start the pinned
   receiver. An older live Runtime will not accept the new Gateway's zero-epoch watch until restarted;
   the later Gateway must not revive the Lease protocol as a fallback for those processes. Then
   remove connection-Lease claims, call-epoch checks and the per-Sandbox `<sandbox>-control`
   EndpointSlice. Keep old Lease resources and permissions available through the Gateway image
   rollback window.
3. **Final compatibility cleanup.** After the epochless Gateway image is pinned, remove remaining
   connection-Lease RBAC and epoch-protocol fields. Dispatcher delivery Leases are unchanged.

Rollback the Controller to Lease reads only with the old Gateway fact writer and codec restored as a
pair. Until Gateway pinning, rollback to the old Gateway also requires a Runtime image accepting its
epoch-bearing calls and the old Lease/EndpointSlice resources and permissions. Restore removed
RBAC/proto compatibility before any later rollback to an old Gateway. A post-cutover Runtime that
has not yet adopted a Controller binding cannot be made compatible by falling back to Gateway
identity; restart it with the compatible receiver and let the Controller bind the current UID.

## Acceptance

Acceptance must cover authenticated Controller-only initial binding, same-UID reconnect, refusal to
change UID after adoption begins or an uncertain callback, stale callbacks/snapshots, idle reset,
Sandbox/incarnation/boot fencing, epochless calls and watches against the bound UID, and legacy
nonzero Gateway behavior for that UID. It must also verify watch loss does not cancel epochless calls
and uncertain mutations are not replayed. The follow-on multi-replica system coverage must exercise
failover, Runtime restart, same-name replacement and independent Dispatcher delivery.
