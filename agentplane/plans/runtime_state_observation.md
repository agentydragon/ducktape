# Runtime state observation

## Proposed ownership

The leader-elected Sandbox Controller manager observes each accepted current Environment Runtime
directly and keeps its latest facts in memory, keyed by Sandbox UID, runner Pod UID and boot ID. For
a VM, the incarnation Pod UID is the virt-launcher Pod UID. A local watch generation changes whenever
a stream is replaced; callbacks must match both that generation and the current identity before
updating facts or timers.

On leadership acquisition, the manager relists current Sandboxes and workload incarnations, then
opens fresh streams. It trusts no old in-memory facts: connectivity and lifecycle decisions wait for
the first authenticated snapshot matching the Sandbox UID, Pod UID and a nonempty boot ID. Idle
timers begin from the first fresh snapshot, never from the runner's clock. A new leader or a reopened
stream resets the controller's in-memory idle timer, so time without observation cannot autosuspend
a Sandbox.

## Protocol boundary

The current `Runner.WatchState` is an exclusive Gateway observer, not a passive facts feed. Its
`ConnectionEpoch` installs the active Runtime watch and fences every Gateway unary, admission and
Executor call. The Gateway claims the per-Sandbox connection Lease to allocate that epoch, watches
Lease changes to reject superseded local connections, and publishes the Lease UID and epoch on the
Gateway-owned EndpointSlice. The Runtime currently authenticates the Gateway SVID; the Sandbox
Controller has a bearer route to the Gateway, not a Runtime client SVID.

Calling the existing `WatchState` from the Controller as-is would replace the active Gateway epoch
and make its calls fail. The proposed design therefore adds an observe-only mode to `WatchState`,
using the existing snapshot facts and a dedicated narrow Controller SVID over a direct
Controller-to-Runtime network path. The passive stream validates the current Sandbox and incarnation
but never installs or advances the Gateway call epoch. No Runtime-to-Gateway callback or reverse dial
is introduced.

The Gateway continues to own its connection Lease, active call epoch and EndpointSlice through the
reader migration. The Controller continues to send `Quiesce` through the Gateway. EndpointSlice
ownership and the Dispatcher delivery Lease are separate choices and stay out of scope here.

## Loss and lifecycle facts

Use the current 10-second snapshot heartbeat and 30-second observation timeout as the initial
freshness window. On stream loss, retain the last same-incarnation snapshot as stale for status
continuity, retry after one second, and reset the idle timer. Stale facts do not prove connectivity,
quiescence or idleness. A fresh matching snapshot restores observation; after the grace window the
Controller reports the Runtime disconnected. A Pod UID or boot ID change starts a new facts record
and timer.

Persisted `QuiesceAttempt` state and its retry deadline survive leader changes and stream loss. Do
not clear it, stop the Pod, or release the state volume from a stale `quiesced` fact. A fresh report
for the current incarnation can complete the seal; the runner's journal and seal acknowledgements
remain on its state volume while observation reconnects. Keep terminal bootstrap outcomes terminal
for that incarnation across reconnects, and never rerun a succeeded, failed or interrupted bootstrap
because a stream was lost. A replacement incarnation starts its own bootstrap state.

## Migration boundary

1. **Reader migration:** add the passive stream and move Sandbox Controller connectivity, bootstrap,
   quiesce, idle, setup and autosuspend-blocker reads to its in-memory facts. Keep the Gateway's
   Lease fact writes during this phase. The Gateway still reads holder, renewal and transition fields
   to claim ownership and reject stale local connections; it also checks the Lease UID and epoch
   before publishing its EndpointSlice. Those readers and the connection Lease remain unchanged.
   The Dispatcher delivery Lease is a separate protocol and is untouched.
2. **Lease writer and package removal:** after all fact readers move, remove fact publication and
   the shared facts codec. Removing the connection Lease itself also requires replacing its Gateway
   replica call fence and the ownership checks used when publishing the EndpointSlice. The existing
   [Gateway ownership decision](task_dag.md#runner_gateway_replica_ownership_design--reconsider-per-runner-gateway-ownership) must select
   those replacements first; one candidate is allowing every authenticated Gateway replica to serve
   Runner calls while retaining an independently owned EndpointSlice. Do not delete the connection
   Lease or its epoch protocol as part of reader migration.
3. **RBAC and rollout:** include the Controller SVID trust and direct network path with reader
   migration. Remove its Kubernetes permission to read connection Leases in a later rollout after
   the relevant image references are pinned.

**Review question:** approve direct, read-only observation through an observe-only `WatchState` mode
with a dedicated Controller SVID, while the Gateway retains its Lease-backed proxy epoch and
EndpointSlice? If not, keep Controller fact reads on the Lease. Separately, the Gateway ownership
decision must name a replacement call fence and EndpointSlice owner before the connection Lease can
be removed.
