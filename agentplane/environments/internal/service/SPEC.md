# EnvironmentService SPEC

## Purpose

Own Sandbox creation, templates, management mutations, discovery and observation in the Environments
binary. List/get/watch return facts needed to choose a target and prepare a Session
request. All read and watch methods return the same `EnvironmentDescriptor` contract. `ResumeEnvironment`
delegates guarded mutation to the lifecycle manager. Creation and policy mutations use private
Kubernetes access; no method attaches a Session or grants authorization.

## Descriptor shape

Every list, get, and watch descriptor groups its facts by purpose. `configuration` contains the
selected template and Session setup values, including workspace guidance, instructions, runtime
choices, bootstrap, preset, and wake/idle-suspend policy. `observed_state` contains canonical
controller-reported readiness, operating mode, phase, incarnation, and idle-suspend facts.
`actions` contains caller-specific resume availability and the eligibility, generation, and token
used to request an explicit resume. `kubernetes_diagnostics` retains the raw Sandbox status and
current-incarnation Pod projection, including the raw Pod status consumed by the frontend's
conditions and container diagnostics. Raw Kubernetes status supplements `observed_state`; it does
not replace its canonical fields.
`identity_name` projects the Sandbox spec's selected Identity name, if any. It is a requested
binding, not proof that a current or prior runtime used that Identity.

A Firecracker Sandbox retains its launcher Pod and incarnation UID while RAM is checkpointed.
`stopStage: Stopped` therefore projects `Suspended` even with an incarnation; `Suspending`
applies only until that checkpoint is reported. The same Pod projection remains available for
diagnostics while suspended.

## Caller and object authorization

- List, get, both watches and explicit resume admit workload SVIDs naming the exact live Identity.
  Exact service SVID grants independently admit readers, managers, launchers and operators.
  Kubernetes bearer credentials are rejected, including mixed SVID/bearer requests. The receiver
  performs no TokenReview. Typed Actions service delegation is opted into by exact full method name;
  its effective Identity never inherits the actor's direct roles.
- Identity inventory is limited to Sandboxes whose `spec.identity.name` names that Identity.
  Read permission grants no Dispatcher write or Runtime proxy access; explicit resume requires
  its separate lifecycle grant.
- Identity access follows the Sandbox's name-based `spec.identity` binding: the current live
  same-name Identity may act, while a credential naming an older UID is rejected at admission.
  This read path does not infer or migrate retained Sessions, Connections, or grants.
- Exact configured service readers may read the entire namespace, including creation and deletion
  before or after an execution account exists. This grant never crosses Identity delegation.
- Resume independently checks the exact target and explicit authority through the lifecycle manager.
  An Identity needs a matching `environment` rule with `resume`; it cannot override an operator stop.
  Direct configured service operators keep requested-stop resume. Read permission alone does not
  authorize resume. Ordinary ServiceAccount self-resume remains retired. Missing grants are hidden
  as `NOT_FOUND` when the Identity cannot read the target, otherwise `PERMISSION_DENIED`. A matching
  grant can authorize a resume without granting inventory access.
- Management methods require an exact service SVID explicitly listed as a manager.
  The Action Service may be listed separately as a launcher, which permits only Create with a
  complete Action caller and group origin. Delegation, a read grant, and a launcher grant never
  confer general management. The service records both the authenticated broker and launch origin
  in create intent, so an identical retry can recover the same Sandbox while a different caller or
  request cannot adopt its name. Create returns the persisted object's exact target handle. A
  successful write retains the UID acknowledged by Kubernetes even if the name is later replaced;
  an uncertain write succeeds only after the same create intent is found in the live object.
- Suspend, delete, and policy changes require a complete namespace/name/UID handle and fence writes
  on the live UID and Kubernetes resourceVersion. Delete requires reported suspension. Uncertain
  outcomes stay uncertain; callers reconcile the same intent before deciding whether to retry.
- A manager may change or clear `spec.identity` with an optimistic, UID/resourceVersion-fenced
  Kubernetes patch. The CRD's CEL admission rule is the only stopped-state gate: the current
  Sandbox must report `stopStage: Stopped` (or have no first status report). Retained Sessions
  do not prevent rebinding and keep their recorded Identity. Dispatcher rechecks the live Identity
  at admission, placement and command delivery; their next command fails `execution_identity_changed`.
  The selected Identity is revalidated by UID during the patch, but deletion and same-name recreation
  after that read remain a cross-resource race because the Sandbox spec stores only a name.
- Workspace and state volumes carry over a rebind: the next Identity reads whatever the previous one
  left, including privileged files. Accepted risk (operator, 2026-11-10); the app warns when an
  identity change is requested (`app/frontend/sandboxes/identity.tsx`).
- Get and watch require the complete Sandbox namespace/name/UID handle. Missing, replaced, deleting, or foreign targets are hidden as `NOT_FOUND` for Identity callers.
  Broad readers may see a deleting target and targets whose execution account is not yet present.
- Identity list filters the configured
  namespace to current Sandboxes bound to that Identity and uses the last visible Sandbox name as a
  lexical cursor; the cursor only skips entries and cannot widen visibility.
  Identity descriptors omit execution-account and automatic-wake authority facts. Resume eligibility,
  generation and token are exposed only while an explicit grant currently authorizes resume. Broad direct readers
  may continue a page using the Kubernetes continuation token because their grant covers that
  namespace.

Delegated calls keep the Actions actor separate from the effective Identity. They receive the same
current `spec.identity` name and UID checks as direct Identity calls. The dispatcher still rechecks
identity at Session admission. Discovery is not an authorization lease and is not a substitute for
mutation-time checks.

## Runtime diagnostics

`GetRuntimeDiagnostics` is the App-only live diagnostic read described in the
[Environments contract](../../SPEC.md#live-runtime-diagnostics). It uses replica-local fenced
Runtime transport and uncached Sandbox incarnation checks, separately from inventory and lifecycle
roles. The read has no wake or mutation effect.

## Snapshot reads and watches

`GetEnvironment` returns the current descriptor without waiting for a condition.
`GetEnvironmentIdentityRecord` reads the current Sandbox-owned WorkloadCredential on demand. It
returns an Identity subject only when the credential has a matching controlling owner reference
for the exact Sandbox UID. A missing credential, a retained legacy record without an Identity, or
an unrelated owner gives an empty record; read failures are reported as unavailable. Credentials are revoked as workloads
stop, and no retained series is available, so this endpoint is not historical proof. The
Environment watches do not subscribe to WorkloadCredential changes; clients refresh this read
explicitly to see a later credential.
`WatchEnvironment` sends an initial `WatchEnvironmentResponse.environment` and subsequent complete
snapshots when descriptor facts change. Changes only to `observed_at` or unprojected Kubernetes
metadata do not emit a new snapshot. There is no readiness predicate or server-side readiness wait.

Observation depends on the control plane, not on a connection to the environment Runtime;
suspended targets remain observable. Target and namespace Pod watches are established before reading
the initial snapshot. Broad direct readers also watch execution ServiceAccounts. Kubernetes
collection resource versions bridge list/watch setup; notifications trigger uncached reads and the
same live UID/ownership checks as get. The service rechecks target UID and Identity ownership after
assembling a descriptor. An already admitted Identity stream never follows a replacement target or
Identity. Credentials are reviewed before sends and periodically while idle; a valid credential
keeps its stream open.
Before returning a target-read error, the service reviews the admitted credential again;
if that review fails, its error ends the stream. Identity deletion or replacement that also
hides the target therefore ends a delegated stream with `PERMISSION_DENIED`.

This is a current-state subscription, not retained transition history. Intermediate states may be
coalesced. Reconnecting starts with a current snapshot and has no cursor. Source closure, expiration,
or failure ends the RPC with `UNAVAILABLE`; a client must reconnect to refresh its observation.
Missing, replaced or no-longer-accessible targets end with `NOT_FOUND`; deletion does so for ordinary callers. Last observations
remain observations of their recorded time; silence or disconnection does not assert readiness.
Client cancellation ends the stream and all source watches. Server writes have a five-second
backpressure bound. A blocked write ends the RPC with `DEADLINE_EXCEEDED` and is not retried.

## Collection watch

`WatchEnvironments` begins with the caller-visible descriptor set in bounded snapshot chunks followed
by `snapshot_complete`. A client replaces its prior set only after completion, including for an
empty snapshot; it discards incomplete snapshots on disconnect. After the initial snapshot, `upsert`
replaces one descriptor and `removed` removes the given namespace, name, and UID. A same-name
replacement removes the prior UID before upserting the new one. Visibility loss also removes an item.
Intermediate changes may be coalesced. Reconnecting starts a fresh snapshot; there is no replay cursor
or transition history. The assembled view draws from multiple Kubernetes resources and does not
promise a globally atomic point-in-time read.

The service starts Sandbox and Pod watches before reading the initial snapshot, and broad direct
readers additionally start account watches. It rereads affected Sandboxes on notifications. It indexes current incarnation Pod names, including VM
Pods; unrelated Pod events do not reread the inventory. Ordinary callers see only their exact live
owned target; explicitly granted direct readers see all targets, including creation and deletion
before or after the account exists. A missing or stale Pod is never substituted for the
controller-reported incarnation. Source loss ends the stream with `UNAVAILABLE`; the client
reconnects for a fresh snapshot. The admitted credential is reviewed before each frame and
periodically while idle, using the resolver's verdict cache. An expired or revoked credential ends
the stream; a successful review leaves it open. A stalled send is bounded by five seconds.

## Returned facts

- `target` is the Sandbox object's immutable identity; a runner Pod or VM incarnation UID is separate.
- Diagnostic fields include creation time, bootstrap binding, preset, controller lifecycle phase, idle
  suspension settings and blockers, raw Sandbox status, and the current Pod. Pod diagnostics are
  present only when name, namespace, and UID match the controller-reported incarnation.
- `execution_account` is resolved from the live controller-owned ServiceAccount only for broad
  readers. Identity callers do not receive this component-owned fact.
- Readiness and operating state come from the current Sandbox status; `observed_at` records when the
  descriptor was assembled.
- Explicit-resume eligibility checks a direct service's exact SVID and configured operator
  authority, or an Identity's live UID and current explicit grant. Identity descriptors expose
  `ALLOWED_IDENTITY_GRANT`, wake generation and token only while authorized. A policy-reader outage
  omits those optional facts without denying implied self-reads; explicit resume fails closed as
  `UNAVAILABLE`. Deleting environments remain visible to broad readers without resume eligibility
  or a token. Idle watches refresh caller-specific facts at the credential recheck interval.
- Automatic wake eligibility remains a separate assessment using `wakeOnSend` and Environments'
  configured wake-caller policy. Dispatcher configures its own policy for its in-process wake manager.
- The descriptor's wake generation and opaque resume token bind an ordinary resume precondition to
  the observed Sandbox UID and generation. The token is not authority; the manager rechecks current
  identity, policy and state at mutation time.
- Runtime choices and workspace guidance are labeled `DEPLOYMENT_CONFIG` and apply only to an exact
  template included in EnvironmentService's published runtime catalog.
- Workspace guidance reports whether the configured root is enforced as a cwd boundary for that
  template. Discovery does not add a cwd restriction.
- Unknown custom templates keep workspace and runtime choices `UNAVAILABLE`; known templates with
  no configured workspace path or runtime choices report `ABSENT`.
- Instructions are `ABSENT`; no bootstrap script or platform prompt is reclassified as target
  instructions.
- Workspace guidance does not validate cwd. The Runner's existing behavior creates the requested
  cwd directory when needed; existing path policy remains owned by the runtime.

## Pagination and errors

`ListEnvironments.limit` is required and ranges from 1 through 100. Identity
callers page over visible Sandboxes with a lexical name cursor; it cannot grant access to another
Sandbox. Broad readers can continue namespace pages; their grant permits seeing all names.

Malformed requests and nonempty cursors from ordinary callers fail `INVALID_ARGUMENT`. Object authorization failures fail
`NOT_FOUND`. Kubernetes reads for the exact candidate or its live ownership fail closed with
`UNAVAILABLE` rather than returning partial facts.

## CLI

`agentplane environments list`, `get` and `resume` are documented in the
[Agent CLI](../../../docs/agent_cli.md#commands).

## Resume and ownership

`ResumeEnvironment` uses the shared [lifecycle contract](../../lifecycle/SPEC.md), including exact
exact service operator grants, Identity policy checks, generation-bound preconditions
and unknown outcome handling. List/get/watch remain read-only; resume permission is
evaluated independently.
`REQUESTED` means the desired running state was accepted, not that the environment is ready. The
Sandbox Controller remains responsible for startup and readiness.

Direct service SVIDs have independent, exact-SPIFFE-ID grants through
`service_readers`, `service_managers`,
`service_launchers`, and `service_operators`. Managers may create directly;
launchers may create only with an Action launch origin. Neither role implies the other. Service operators may
explicitly resume, including a requested stop; delegated callers never inherit those roles.
`service_delegation` admits a service actor only for configured read methods and
`ResumeEnvironment`, using the live effective Identity and its explicit resume grants. Delegation
never supplies operator authority or waives the resume token. This service's `wake_service_callers`
list grants automatic broker eligibility for EnvironmentService assessments only. Dispatcher has a
separate list and owns service wake budgets keyed by exact SPIFFE ID. Create receipts retain the
typed service actor separately from the Action origin.
