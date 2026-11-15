# Runtime proxy connection and authorization contracts

The implementation is shared during the receiver migration. Gateway retains its existing endpoint
and authentication. Optional Environments registration uses the same fencing and forwarding, on
its every-replica listener with independent routing caches. Its method grants admit only the four
configured service-role allowlists; no bearer, workload Identity or delegation is granted. Its
outbound Runtime peer is `spiffe://agentplane/runtime-proxy`. The Gateway registration remains until
callers have moved. `environments/runtimeproxy` is the final package home; retirement removes the
Gateway import and its temporary Bazel visibility grant without moving these files again.

Environments uses shared service-only admission without a TokenReview resolver or proxy bearer
audience. Bearer metadata is refused even alongside an admitted service SVID. The retained
Gateway's separately configured bearer rules remain during the caller migration.

## Runtime observation and independent Gateway connections

Every Gateway replica resolves the current Sandbox and workload independently and opens a direct
`WatchState` to the Runtime on private port `9090`. It uses the Sandbox UID and current workload Pod
UID from Kubernetes and requires the response to match both identities and return a nonempty Runtime
boot ID. A VM's workload Pod is its current virt-launcher Pod. A watch can only observe the
incarnation already bound by the Sandbox Controller's authenticated direct watch; the Gateway
cannot establish or advance that binding. Each replica can then serve the Runtime connection
locally.

Each forwarded RPC is fenced by the current Sandbox UID, workload Pod UID and Runtime boot ID.
Those values come from the Gateway's live Kubernetes inventory and its authenticated Runtime
observation, never from caller metadata. Runtime rejects a stale Sandbox, workload incarnation or
process boot before admitting work. Loss of one Gateway's observation closes that replica's local
connection; it does not revoke another replica's connection or cancel Runtime-owned admitted work.
An uncertain mutation is never replayed after reconnect or failover.

Callers connect through the stable `runner-gateway` Service. The per-Sandbox `<sandbox>-control`
EndpointSlice and Gateway-owned connection Lease are not part of the route. Dispatcher delivery
Leases continue to coordinate queue delivery independently.

The Runtime protocol carries no Gateway connection epoch. Runtime call authorization uses the
Controller-bound Sandbox UID, workload UID and Runtime boot ID, independently of Gateway observer
lifetime. Losing one replica's observation does not revoke another replica's calls or cancel
Runtime-owned admitted work.

## Environment Runtime proxy authorization

The public Gateway authenticates exact service SVIDs. Kubernetes bearer tokens and delegated
envelopes grant no proxy or Executor admission. All calls retain Sandbox UID/owner and current
Runtime incarnation and boot fencing. Callers may additionally provide one non-empty
`x-agentplane-incarnation-uid` metadata value to require a specific Runtime incarnation. The Gateway
resolves and validates the current Sandbox and Pod first, then returns `Aborted` without forwarding
if that incarnation differs. A duplicate or empty value is `InvalidArgument`.

- `runtime_proxy_session_callers` grants Runtime `CreateSession`, `SubmitInput`, `ChangeModel`,
  `ChangeReasoningEffort` and `InterruptTurn` to exact Sessions service actors. Dispatcher workers
  present their Sessions certificate across replicas; they replace outgoing metadata and never
  forward a caller's credentials. There is no startup TokenReview or own-Pod authentication read.
- `runtime_proxy_readers` grants `ListSessions` and `GetStatus` diagnostics, without command or wake
  authority. `WatchState` remains private to Gateway-to-Runtime observation.
- `runtime_proxy_controller_callers` grants `Quiesce`, `PrepareSuspend` and `ResumeAdmission`.
- `executor_service_callers` independently enables the Executor methods, including `OpenSession`.
  This does not create a Runtime Session or enter Dispatcher admission.
- Every grant defaults empty. Service roles carry no fabricated account UID or Pod identity.
  Dispatcher delegation and lifecycle wake authorization remain separate boundaries.

Runtime Session creation and commands, including queued interrupts, are writes: the Gateway
refuses forwarding once its synchronized inventory observes `Quiescing` or deletion. Diagnostics
remain available while quiescing, and the controller's authorized `Quiesce` can still complete the
seal. Cache observation is asynchronous; this routing check does not make Kubernetes
`deletionTimestamp` atomic with Runtime admission.

For ordinary managed deletion, the receiver barrier is the exact incarnation's completed Runtime
quiescence: every Session has closed final admission, sealed its journal and received History's seal
acknowledgement before the controller removes the workload. A request already past Gateway routing
must still pass Runtime's admission gate. Runtime's initial lameduck permits exact-turn interrupts;
its final Session cutoff refuses new command IDs, including interrupts. Existing command IDs retain
journal reconciliation. Removing finalizers or using foreground deletion bypasses this managed
protocol and supplies no such proof.

## Service certificate callers

The Runtime proxy has independent exact-service allowlists for diagnostics readers, Sessions
operations, and Controller lifecycle operations. Executor has its own service allowlist. No shared
Gateway service identity grants all four roles. The existing combined serving identity remains the
server pin; Sessions, runtime-proxy and Environments client identities follow those roles across
the planned binary split. Verified service certificates carry neither Kubernetes account nor Pod
proof, and method denial never falls back to bearer verification. All lists default empty. Legacy receiver settings remain ignored and decodable until their
manifest removal and rollout complete (stage 5 cleanup, 2026-11-15).
