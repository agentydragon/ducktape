# Gateway connection and authorization contracts

## Runtime observation and independent Gateway connections

Every Gateway replica resolves the current Sandbox and workload independently and opens a direct
`WatchState` to the Runtime on private port `9090`. It uses the Sandbox UID and current workload Pod
UID from Kubernetes and requires the response to match both identities and return a nonempty Runtime
boot ID. A VM's workload Pod is its current virt-launcher Pod. A zero or absent watch epoch can only
observe the incarnation already bound by the Sandbox Controller's authenticated direct watch; the
Gateway cannot establish or advance that binding. Each replica can then serve the Runtime connection
locally. Gateway availability does not depend on per-Sandbox Lease ownership, EndpointSlices or an
epoch allocator.

Each forwarded RPC is fenced by the current Sandbox UID, workload Pod UID and Runtime boot ID.
Those values come from the Gateway's live Kubernetes inventory and its authenticated Runtime
observation, never from caller metadata. Runtime rejects a stale Sandbox, workload incarnation or
process boot before admitting work. Loss of one Gateway's observation closes that replica's local
connection; it does not revoke another replica's connection or cancel Runtime-owned admitted work.
An uncertain mutation is never replayed after reconnect or failover.

Callers connect through the stable `runner-gateway` Service. The per-Sandbox `<sandbox>-control`
EndpointSlice and Gateway-owned connection Lease are not part of the route. Dispatcher delivery
Leases continue to coordinate queue delivery independently.

The generated Runtime protocol still carries `connection_epoch` for the old Gateway rollback
window. Legacy nonzero watch and call epochs remain accepted by the receiver for the already-bound
incarnation, but this Gateway sends no allocated epoch. Remove that receiver compatibility and its
RBAC only in the follow-on cleanup after the old Gateway rollback window closes.

## Environment Runtime proxy authorization

The public Gateway authenticates Gateway-audience Kubernetes tokens with singular Pod-name and
Pod-UID proof. All calls retain Sandbox UID/owner and current Runtime incarnation and boot fencing.
Callers may additionally provide one non-empty `x-agentplane-incarnation-uid` metadata value to
require a specific Runtime incarnation. The Gateway resolves and validates the current Sandbox and
Pod first, then returns `Aborted` without forwarding if that incarnation differs. A duplicate or
empty value is `InvalidArgument`; omitting the value preserves the ordinary proxy behavior.

- Only Pod-bound tokens of the combined Gateway/Dispatcher ServiceAccount may call Runtime `CreateSession`, `SubmitInput`,
  `ChangeModel`, `ChangeReasoningEffort` and `InterruptTurn`. Startup derives its account from the
  existing mounted delivery token, requires it in `gateway_callers`, and verifies the configured
  current Pod namespace/name, Kubernetes Pod UID and Pod ServiceAccount. An unreadable token, unavailable
  TokenReview or Pod read, missing Pod proof or identity mismatch prevents startup.
- Dispatcher workers use that workload's projected token across replicas. They replace outgoing
  metadata; caller credentials and caller-supplied identity headers cannot become this proof.
  Every other caller uses SessionDispatcher for Session creation and commands, with its existing
  direct or delegated authorization. The workload credential trusts the combined process, since
  Gateway and Dispatcher share one Pod and executable; it is not a separate in-process identity. Any Pod able to obtain that ServiceAccount
  is trusted equally, including other Gateway replicas. Existing platform principals allowed to
  create arbitrary Pods remain trusted Kubernetes administrators; this policy does not constrain
  that capability.
- `gateway_callers` grants `ListSessions` and `GetStatus` diagnostics. Diagnostics grant neither
  command nor wake authority. `WatchState` remains private to Gateway-to-Runtime observation.
- Only `sandbox_controller_account` may `Quiesce`; omitting it admits nobody.
- The separate Executor service retains its own `executor_callers` grant, including its streaming
  `CreateSession`. It does not create a Runtime Session or enter Dispatcher admission.
- Gateway proxy and Executor methods reject delegated envelopes. Delegation is configured only
  on Dispatcher and EnvironmentService methods. Explicit resume and automatic wake retain their
  own lifecycle authorization.

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

## Service certificate callers during the cutover

The Runtime proxy has independent exact-service allowlists for diagnostics readers, Sessions
operations, and Controller lifecycle operations. Executor has its own service allowlist. No shared
Gateway service identity grants all four roles. The existing combined serving identity remains the
server pin; Sessions, runtime-proxy and Environments client identities follow those roles across
the planned binary split. Verified service certificates carry neither Kubernetes account nor Pod
proof, and method denial never falls back to bearer verification. All new lists default empty;
manifest settings and caller switches follow receiver image pins.
