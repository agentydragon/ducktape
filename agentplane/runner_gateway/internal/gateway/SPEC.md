# Gateway connection and authorization contracts

## Independent Runtime observations

Every authenticated Gateway replica may independently observe and call one Runtime. The Sandbox
Controller binds its Sandbox UID and workload Pod UID through its dedicated observe-only watch;
Gateway watches cannot establish or advance that binding. Gateway watches send no connection epoch.
Calls carry the bound Sandbox UID, incarnation UID and boot ID. Runtime rejects stale fences before
admission. Removing one replica's observation cannot revoke another replica's calls.

Observation loss removes only that replica's local callable connection. Reconnect validates the
current workload and learns its boot ID again. Replacement observers wait for their canceled
predecessor to exit before installing a new local connection. Admitted Runtime work remains owned
by Runtime; an uncertain mutation is never automatically replayed.

## Legacy route handoff

The connection Lease and `<sandbox>-control` EndpointSlice temporarily select a destination for
existing callers. Their epoch is a Kubernetes publication fence only; it is never sent to Runtime
and grants no call authority. Each replica becomes callable before attempting route publication,
so another replica's route claim or a publication error does not block its direct calls.

Orderly shutdown closes local traffic before releasing its exact route claim, fenced by Sandbox
owner, Lease UID, holder and route epoch with resource-version conflict detection. A delayed release
cannot clear a successor. Contenders measure expiry on their own monotonic clocks. Route publication
is retried on snapshots and heartbeats. The Sandbox Controller consumes lifecycle facts directly;
route records prove neither readiness, idleness nor quiescence.

## Environment Runtime proxy authorization

The public Gateway authenticates Gateway-audience Kubernetes tokens with singular Pod-name and
Pod-UID proof. All calls retain Sandbox UID/owner and current Runtime incarnation fencing.
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
