# EnvironmentService SPEC

## Purpose

Own Sandbox creation, templates, management mutations, discovery and observation in the existing
Runner Gateway process. List/get/watch return facts needed to choose a target and prepare a Session
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

- Methods admit a direct TokenReview-verified ServiceAccount, or (for list/get only) a workload
  credential naming an Identity. The composition root may add Actions delegation to selected
  methods through the Runner Gateway's existing Dispatcher audience and served namespace set.
  `WatchEnvironment`, `WatchEnvironments`, and mutations retain their existing direct
  ServiceAccount gates. `ResumeEnvironment` is separately authorized through the lifecycle
  manager; read permission grants no Dispatcher write or Gateway proxy access.
- Handlers read typed workload authentication. The ServiceAccount path requires its exact live UID.
  The Identity path requires the exact live Identity name and UID and is limited to Sandboxes whose
  `spec.identity.name` names that Identity. It never receives the delegated actor's component
  reader grants.
- Identity access follows the Sandbox's name-based `spec.identity` binding: the current live
  same-name Identity may act, while a credential naming an older UID is rejected at admission.
  This read path does not infer or migrate retained Sessions, Connections, or grants.
- A directly authenticated ServiceAccount explicitly listed in `environment_readers` may read the entire configured
  Sandbox namespace. This grant does not confer mutation or wake authority and never passes through
  delegation, including delegation to an Identity.
- Resume independently checks the exact target and execution account, `resumePolicy`, and direct
  operator authority through the lifecycle manager. Read permission alone does not authorize resume.
- Management methods require a direct Pod-bound ServiceAccount explicitly listed as a manager.
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
- A manager may change or clear `spec.identity` only before first status report or while the
  current Sandbox reports `stopStage: Stopped`; Kubernetes admission is the final stop check.
  EnvironmentService holds Dispatcher's exclusive per-Sandbox admission lock across a read of
  retained Sessions and the Kubernetes patch. Any retained Session placed on that exact Sandbox
  UID whose execution Identity name or UID differs is a refusal. Dispatcher currently has no
  terminal lifecycle fact that proves a retained Session is no longer resumable, so this may
  refuse a historical or failed Session. CreateSession holds the shared form of that lock and
  rechecks the live Sandbox and execution Identity after acquiring it. The selected Identity is
  revalidated by UID during the patch, but deletion and same-name recreation after that read
  remain a cross-resource race because the Sandbox spec stores only a name.
- Get and watch require the complete Sandbox namespace/name/UID handle. Missing, replaced, deleting, or foreign targets are hidden as `NOT_FOUND` for ordinary callers.
  Broad readers may see a deleting target and targets whose execution account is not yet present.
- Account-scoped callers resolve their same-name Sandbox candidate. Identity list filters the
  configured namespace to current Sandboxes bound to that Identity and uses the last visible
  Sandbox name as a lexical cursor; the cursor only skips entries and cannot widen visibility.
  Identity descriptors omit execution-account and lifecycle authority facts. Broad direct readers
  may continue a page using the Kubernetes continuation token because their grant covers that
  namespace.

Delegated calls keep the Actions actor separate from the effective caller. ServiceAccount calls
receive the same live Sandbox-owned account UID checks as direct calls; Identity calls receive the
same current `spec.identity` name and UID checks as direct Identity calls. The dispatcher still
rechecks identity at Session admission. Discovery is not an authorization lease and is not a
substitute for mutation-time checks.

## Snapshot reads and watches

`GetEnvironment` returns the current descriptor without waiting for a condition.
`GetEnvironmentIdentityRecord` reads the current Sandbox-owned WorkloadCredential on demand. It
returns an Identity subject only when the credential has a matching controlling owner reference
for the exact Sandbox UID. A missing credential, ServiceAccount subject, or unrelated owner gives
an empty record; read failures are reported as unavailable. Credentials are revoked as workloads
stop, and no retained series is available, so this endpoint is not historical proof. The
Environment watches do not subscribe to WorkloadCredential changes; clients refresh this read
explicitly to see a later credential.
`WatchEnvironment` sends an initial `WatchEnvironmentResponse.environment` and subsequent complete
snapshots when descriptor facts change. Changes only to `observed_at` or unprojected Kubernetes
metadata do not emit a new snapshot. There is no readiness predicate or server-side readiness wait.

Observation depends on the control plane, not on a connection to the environment Runtime;
suspended targets remain observable. Target, execution-ServiceAccount, and namespace Pod watches are established
before reading the initial snapshot. Kubernetes collection resource versions bridge list/watch
setup; notifications trigger uncached reads and the same live UID/ownership checks as get.
The service rechecks target UID and execution ownership after assembling a descriptor.
An already admitted stream never follows a replacement target or execution account. Credentials
are reviewed before sends and periodically while idle; a valid credential keeps its stream open.

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

The service starts Sandbox, account, and Pod watches before reading the initial snapshot, and
rereads affected Sandboxes on notifications. It indexes current incarnation Pod names, including VM
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
- `execution_account` is resolved from the live controller-owned ServiceAccount for account callers
  and broad readers. Identity callers do not receive this component-owned fact.
- Readiness and operating state come from the current Sandbox status; `observed_at` records when the
  descriptor was assembled.
- Explicit-resume eligibility includes a denial reason and is evaluated for the effective account
  caller against its exact live ServiceAccount identity, the Sandbox `resumePolicy`, and applicable
  direct operator authority. Identity callers receive no resume eligibility, wake generation, token,
  or automatic-wake eligibility. Deleting environments remain visible to broad readers without
  explicit-resume eligibility or a resume token.
- Automatic wake eligibility remains a separate assessment using `wakeOnSend` and the configured
  wake-caller policy.
- The descriptor's wake generation and opaque resume token bind an ordinary resume precondition to
  the observed Sandbox UID and generation. The token is not authority; the manager rechecks current
  identity, policy and state at mutation time.
- Runtime choices and workspace guidance are labeled `DEPLOYMENT_CONFIG` and apply only to an exact
  template included in the Gateway's published runtime catalog.
- Workspace guidance reports whether the configured root is enforced as a cwd boundary for that
  template. Discovery does not add a cwd restriction.
- Unknown custom templates keep workspace and runtime choices `UNAVAILABLE`; known templates with
  no configured workspace path or runtime choices report `ABSENT`.
- Instructions are `ABSENT`; no bootstrap script or platform prompt is reclassified as target
  instructions.
- Workspace guidance does not validate cwd. The Runner's existing behavior creates the requested
  cwd directory when needed; existing path policy remains owned by the runtime.

## Pagination and errors

`ListEnvironments.limit` is required and ranges from 1 through 100. The request and response retain
the approved paged contract. The current Sandbox-owned account mapping looks up only the reviewed
account's same-name candidate. Identity callers page over visible Sandboxes with a lexical name
cursor; it cannot grant access to another Sandbox. Broad readers can continue namespace pages; their
grant permits seeing all names.

Malformed requests and nonempty cursors from ordinary callers fail `INVALID_ARGUMENT`. Object authorization failures fail
`NOT_FOUND`. Kubernetes reads for the exact candidate or its live ownership fail closed with
`UNAVAILABLE` rather than returning partial facts.

## CLI

- `agentplane environments list [--limit N] [--after TOKEN]`
- `agentplane environments get NAMESPACE NAME UID`
- `agentplane environments resume NAMESPACE NAME UID --resume-token TOKEN`

All use `AGENTPLANE_SESSIONS_URL`; `AGENTPLANE_SESSIONS_CREDENTIAL` is an optional bearer for callers
whose forwarder requires it. Workload SVID routes need no bearer. A
resume response is acceptance, not readiness, and an uncertain reply is never retried automatically.
Reconcile current readiness and any uncertain effect before creating a new Session or submitting
input. CLI and Action/MCP wait adapters over `WatchEnvironment` remain follow-up work.

## Resume and ownership

`ResumeEnvironment` uses the shared [lifecycle contract](lifecycle/SPEC.md), including exact
ServiceAccount UID checks, policy opt-in, generation-bound preconditions, shared budget and unknown
outcome handling. List/get/watch remain read-only; resume permission is evaluated independently.
`REQUESTED` means the desired running state was accepted, not that the environment is ready. The
Sandbox Controller remains responsible for startup and readiness.
