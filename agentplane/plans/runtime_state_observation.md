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

Any authenticated Runner Gateway replica may connect to and call a Runtime. The selected contract has
no singleton Gateway connection owner or connection epoch. Runtime checks Sandbox UID, workload
incarnation and its own boot ID on each request. Gateway SVID authentication establishes the service
identity; Gateway still enforces caller and per-method authorization. The SVID does not let a replica
choose or advance an incarnation: Runtime must bind the current UID/incarnation/boot tuple from a
Runtime-trusted source and require forwarded metadata to match. Dispatcher delivery Leases remain a
separate mechanism and are out of scope.

The Controller uses a direct passive Runtime state stream with its own narrow SVID; it does not
install or advance Gateway call ownership. `Quiesce` continues through the Gateway. Runtime restarts,
workload replacement and same-name Sandbox replacement invalidate old calls and facts through the
boot ID, incarnation UID and Sandbox UID respectively. Another authenticated Gateway can connect to
the current Runtime without waiting for a connection Lease, but stale callers cannot.

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
   and the shared codec until #16328, which removes them only after the new Controller is running on
   staging. If setting `runner_security` in a manifest, first pin code that accepts it while absent.
2. **Epochless call compatibility.** A separate receiver change makes the epoch optional for proxy
   and Executor calls, while continuing to accept legacy epoch-bearing calls. It does not remove
   exclusive `WatchState` adoption. Pin the compatible Runtime image before changing Gateway
   ownership.
3. **Multi-replica `WatchState`.** In a follow-on, remove exclusive epoch-based observer adoption
   while retaining the Runtime-trusted UID/incarnation/boot binding; pin the receiver image before
   removing Gateway ownership.
4. **Gateway removal.** After the receiver image is pinned, route callers through the stable
   `runner-gateway` Service and remove connection-Lease claims, call-epoch checks and the per-Sandbox
   `<sandbox>-control` EndpointSlice. Keep old Lease resources and authorization available through
   the Gateway image rollback window.
5. **Final compatibility cleanup.** After the epochless Gateway image is pinned, remove remaining
   connection-Lease RBAC and epoch-protocol fields. Dispatcher delivery Leases are unchanged.

Rollback the Controller to Lease reads only with the old Gateway fact writer and codec restored as a
pair. Roll back configuration requiring `runner_security` before rolling back to code that does not
accept it. Until Gateway pinning, rollback to the old Gateway also requires a Runtime image accepting
its epoch-bearing calls and the old Lease/EndpointSlice resources and permissions. Restore removed
RBAC/proto compatibility before any later rollback to an old Gateway.

## Acceptance

Acceptance must cover observer reconnect, stale callbacks/snapshots, idle reset, identity fencing,
Runtime-trusted incarnation binding, optional-epoch calls while legacy `WatchState` adoption remains,
and uncertain outcomes without replay. Later multi-replica system coverage must exercise failover,
Runtime restart, same-name replacement and independent Dispatcher delivery. The doc-only decision
change does not run system tests.
