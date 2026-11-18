# Runtime proxy connection and authorization contracts

Environments replicas serve EnvironmentRuntime and HarnessHost on `environments:8082`.
Executor registers when its own grant is configured. Callers pin
`spiffe://agentplane/sandbox-controller`. The outbound proxy uses `spiffe://agentplane/runtime-proxy`,
separate from the leader's incarnation-binding observer. Sessions reaches hosted HarnessHost
through this proxy.

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

A replacement observer waits for its predecessor to exit before publishing the new connection.
Dispatcher delivery Leases coordinate queue delivery independently of proxy connectivity.

## Destination and deadline

EnvironmentRuntime and HarnessHost callers send each of these metadata keys exactly once, with
a non-empty value:

- `x-agentplane-sandbox`
- `x-agentplane-sandbox-namespace`
- `x-agentplane-sandbox-uid`
- `x-agentplane-sandbox-owner-namespace`
- `x-agentplane-sandbox-owner-name`

The proxy returns `INVALID_ARGUMENT` for missing, repeated or empty values, and `NOT_FOUND` for
a missing Sandbox or a mismatched destination or owner. It checks for a matching ready workload
and callable local connection. Unavailable routing returns `UNAVAILABLE`.

Callers can also supply one non-empty `x-agentplane-incarnation-uid`. An empty or duplicate
value returns `INVALID_ARGUMENT`. After validating the destination, a changed incarnation
returns `ABORTED` before forwarding. Executor carries destination and incarnation in its
[typed request](../../env/runtime/execution/SPEC.md#ownership-and-admission).
Forwarded calls retain the caller's deadline.

## Environment Runtime proxy authorization

Admission uses exact service SVIDs with no TokenReview resolver or proxy bearer audience.
Bearer metadata is refused even alongside an admitted service certificate. Workload Identity
SVIDs and delegation grant no proxy admission.

- `environment_service.runtime_proxy.session_callers` grants HarnessHost `CreateSession`, `SubmitInput`, `ChangeModel`,
  `ChangeReasoningEffort`, `InterruptTurn`, `ListSessions` and `WatchSessions`,
  to exact Sessions service actors. Dispatcher workers present their Sessions certificate across replicas; they replace
  outgoing metadata and never forward a caller's credentials. There is no startup TokenReview or own-Pod authentication
  read.
- `environment_service.runtime_proxy.readers` grants EnvironmentRuntime `GetStatus`
  process/lifecycle diagnostics, without command or wake authority. `HarnessHost.ListSessions` admits the union of `readers` and
  `session_callers`, pairing the existing inventory readers with Sessions' watch grant.
  `WatchState` remains private to proxy-to-Runtime observation.
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

A fresh Executor `OpenSession` may omit `scope.expected_incarnation_uid`. After exact Sandbox
resolution, the proxy verifies the current Runtime connection against the observed incarnation,
sets the forwarded scope to that incarnation, and returns it as `incarnation_uid` in the first
response. This permits initial binding without a caller-supplied incarnation. Resuming an execution
session and every unary operation still require an explicit incarnation fence. Missing, stale or
foreign Runtime connections cannot establish the initial binding; Runtime continues to check the
explicit fence forwarded by the proxy.

## Internal diagnostic reads

EnvironmentService uses `RuntimeStatus` internally with the exact Environment and an incarnation
from an uncached Sandbox read. This reuses the existing ready-Pod route and authenticated watch,
replaces outgoing metadata with the transport fence, and rejects a mismatched response boot ID or
an observation replaced/lost during the read. It grants no extra Runtime method to external callers.

## HarnessHost status version

HarnessHost inventory forwarding requires exactly one initial metadata value
`x-agentplane-harness-host-session-version: 1`. The proxy reads and validates the upstream watch
header before receiving any protobuf frame: older Runtimes serve `SessionSummary` fields that
collide with the new `Session` status. It forwards the verified marker with List and Watch responses;
missing, duplicate or unsupported versions return `FailedPrecondition`. Downstream status readers
must likewise validate the initial header before receiving frames, so an older proxy cannot make
an old Runtime appear compatible. Unavailable status must remain unknown to those readers.

## Contracted Runtime API

EnvironmentRuntime forwards only `GetStatus`, `Quiesce`, `PrepareSuspend` and `ResumeAdmission`;
`WatchState` remains the private observation transport. Session creation, commands and inventory
use HarnessHost exclusively. An old Runtime can keep serving until its Sandbox restarts: its
HarnessHost, lifecycle and diagnostic methods remain compatible. Calls to the removed
EnvironmentRuntime Session paths stop at Environments with `UNIMPLEMENTED` and never reach that
Runtime. An old Runtime's `GetStatus` can still contain field 2 (Session rows); Environments selects
only process, boot and lifecycle diagnostic fields before returning it, including through
EnvironmentService.GetRuntimeDiagnostics. Unknown legacy Session data is not forwarded.
