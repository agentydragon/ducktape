# Runtime proxy connection and authorization contracts

Every Environments replica serves the EnvironmentRuntime proxy and Executor on
`sandbox-controller:8082`, pinning `spiffe://agentplane/sandbox-controller`. The outbound Runtime peer
is `spiffe://agentplane/runtime-proxy`, separate from the leader's incarnation-binding observer.
The Environments binary owns `environments/runtimeproxy`. The Sessions binary at
`agentplane/session_dispatcher/` has no Runtime proxy or Executor receiver and makes no direct Runtime
connection.

Admission uses shared service-only rules without a TokenReview resolver or proxy bearer audience.
Bearer metadata is refused even alongside an admitted service SVID. Workload Identity SVIDs and
delegation grant no proxy admission.

## Runtime observation and independent replica connections

Every Environments replica resolves the current Sandbox and workload independently and opens a direct
`WatchState` to the Runtime on private port `9090`. It uses the Sandbox UID and current workload Pod
UID from Kubernetes and requires the response to match both identities and return a nonempty Runtime
boot ID. A VM's workload Pod is its current virt-launcher Pod. A watch can only observe the
incarnation already bound by the Sandbox Controller's authenticated direct watch; the proxy
cannot establish or advance that binding. Each replica can then serve the Runtime connection
locally.

Each forwarded RPC is fenced by the current Sandbox UID, workload Pod UID and Runtime boot ID.
Those values come from the proxy's live Kubernetes inventory and its authenticated Runtime
observation, never from caller metadata. Runtime rejects a stale Sandbox, workload incarnation or
process boot before admitting work. Loss of one replica's observation closes that replica's local
connection; it does not revoke another replica's connection or cancel Runtime-owned admitted work.
An uncertain mutation is never replayed after reconnect or failover.

Callers connect through the stable `sandbox-controller:8082` Service. The route does not use a
per-Sandbox `<sandbox>-control` EndpointSlice, connection Lease or proxy epoch. Dispatcher delivery
Leases continue to coordinate queue delivery independently.

Runtime call authorization uses the Controller-bound Sandbox UID, workload UID and Runtime boot ID,
independently of proxy observer lifetime. Losing one replica's observation does not revoke another
replica's calls or cancel Runtime-owned admitted work.

## Environment Runtime proxy authorization

The Environments receiver authenticates exact service SVIDs. Kubernetes bearer tokens and delegated
envelopes grant no proxy or Executor admission. All calls retain Sandbox UID/owner and current
Runtime incarnation and boot fencing. Callers may additionally provide one non-empty
`x-agentplane-incarnation-uid` metadata value to require a specific Runtime incarnation. The proxy
resolves and validates the current Sandbox and Pod first, then returns `Aborted` without forwarding
if that incarnation differs. A duplicate or empty value is `InvalidArgument`.

- `environment_service.runtime_proxy.session_callers` grants Runtime `CreateSession`, `SubmitInput`, `ChangeModel`,
  `ChangeReasoningEffort` and `InterruptTurn` to exact Sessions service actors. Dispatcher workers
  present their Sessions certificate across replicas; they replace outgoing metadata and never
  forward a caller's credentials. There is no startup TokenReview or own-Pod authentication read.
- `environment_service.runtime_proxy.readers` grants `ListSessions` and `GetStatus` diagnostics, without command or wake
  authority. `WatchState` remains private to proxy-to-Runtime observation.
- `environment_service.runtime_proxy.controller_callers` grants `Quiesce`, `PrepareSuspend` and `ResumeAdmission`.
- `environment_service.runtime_proxy.executor_callers` independently enables the Executor methods, including `OpenSession`.
  This does not create a Runtime Session or enter Dispatcher admission.
- Every grant defaults empty. Service roles carry no fabricated account UID or Pod identity.
  Dispatcher delegation and lifecycle wake authorization remain separate boundaries.

Runtime Session creation and commands, including queued interrupts, are writes: the proxy
refuses forwarding once its synchronized inventory observes `Quiescing` or deletion. Diagnostics
remain available while quiescing, and the controller's authorized `Quiesce` can still complete the
seal. Cache observation is asynchronous; this routing check does not make Kubernetes
`deletionTimestamp` atomic with Runtime admission.

For ordinary managed deletion, the receiver barrier is the exact incarnation's completed Runtime
quiescence: every Session has closed final admission, sealed its journal and received History's seal
acknowledgement before the controller removes the workload. A request already past proxy routing
must still pass Runtime's admission gate. Runtime's initial lameduck permits exact-turn interrupts;
its final Session cutoff refuses new command IDs, including interrupts. Existing command IDs retain
journal reconciliation. Removing finalizers or using foreground deletion bypasses this managed
protocol and supplies no such proof.

## Internal diagnostic reads

EnvironmentService uses `RuntimeStatus` internally with the exact Environment and an incarnation
from an uncached Sandbox read. This reuses the existing ready-Pod route and authenticated watch,
replaces outgoing metadata with the transport fence, and rejects a mismatched response boot ID or
an observation replaced/lost during the read. It grants no extra Runtime method to external callers.

## Service certificate callers

The Runtime proxy has independent exact-service allowlists for diagnostics readers, Sessions
operations, and Controller lifecycle operations. Executor has its own service allowlist. No shared
service identity grants all four roles. Sessions clients pin the Sandbox Controller server identity;
the proxy authenticates to Runtime with its separate Runtime-proxy identity. Verified service certificates carry neither Kubernetes account nor Pod
proof, and method denial never falls back to bearer verification. All lists default empty.
