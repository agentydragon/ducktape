# EnvironmentService SPEC

## Purpose

Expose discovery, observation and management of Agentplane environments. The service runs in the
Runner Gateway binary alongside the Session Dispatcher, sharing its lifecycle manager. List/get/watch
return facts needed to choose a target and prepare a Session request; `GetEnvironment` returns
immediately and `WatchEnvironment` observes current-state changes. Management creates environments,
lists templates, suspends and deletes targets, and changes wake and idle-suspend settings. Resume
delegates guarded mutation and current authorization checks to the shared lifecycle manager. These
operations do not attach a Session or grant authorization.

## Caller and object authorization

- Read and resume methods admit a direct TokenReview-verified ServiceAccount or an explicitly configured
  Actions delegation using the Runner Gateway's existing Dispatcher audience and served namespace
  set. `ListEnvironments`, `GetEnvironment`, `WatchEnvironment`, and `WatchEnvironments` may be
  added to the Gateway's exact delegated method list. `ResumeEnvironment` is separately authorized
  through the lifecycle manager; these methods do not grant Dispatcher writes or Gateway proxy
  methods.
- The handler obtains the admitted effective caller from shared workload authentication and requires
  a complete ServiceAccount identity, including its UID. Direct calls retain their existing
  reviewed-account path.
- For ordinary callers, `sandboxreader.Reader.ExecutionAccount` rereads the exact live Sandbox UID and
  confirms its non-deleting ServiceAccount has the Sandbox's exact controlling owner reference.
- An ordinary caller receives a result only if namespace, account name, and account UID all match the effective caller.
- A directly authenticated ServiceAccount explicitly listed in `environment_readers` may read the entire configured
  Sandbox namespace. This grant does not confer mutation or wake authority, and does not pass through delegation.
- Resume independently checks the exact target and execution account, `resumePolicy`, and direct
  operator authority through the lifecycle manager. Read permission alone does not authorize resume.
- Get and watch require the complete Sandbox namespace/name/UID handle. Missing, replaced, deleting, or foreign targets are hidden as `NOT_FOUND` for ordinary callers.
  Broad readers may see a deleting target and targets whose execution account is not yet present.
- The page request/response remains compatible with bounded pagination. Current Sandbox-owned
  accounts resolve by the account's same-name Sandbox candidate, so the service emits no cursor.
  Ordinary callers reject nonempty cursors and never receive namespace-wide Kubernetes tokens. Broad readers
  may continue a page using the Kubernetes continuation token because their grant covers that namespace.

Delegated calls keep the Actions actor separate from the effective caller. They receive the same
live Sandbox-owned ServiceAccount UID checks as direct calls. The dispatcher still rechecks
identity at Session admission. Discovery is not an authorization lease and is not a substitute for
mutation-time checks.

Management methods require a direct, Pod-bound ServiceAccount listed in `environment_managers`;
read access and delegation do not grant management access. A separately configured direct Pod-bound
`environment_launchers` account may create an environment only when the request identifies its
Action caller, Action group, and launcher grants. This grant does not authorize other management
operations. The app uses its manager identity to proxy its authenticated requests; the Action Service
uses the launcher identity for approved launches and owns its separate PolicySet and PolicyBinding
grants. Create applies the Gateway's default PolicySets and records caller and request intent to
support idempotent adoption; different intents cannot adopt the same name. Mutations require the
target UID and preserve Kubernetes resource-version fencing. The Sandbox Controller reconciles
the resulting desired state.

## Snapshot reads and watches

`EnvironmentDescriptor` is the canonical environment view for all service reads and watch frames. It
keeps caller-facing target, execution account, readiness, resume, workspace, and runtime guidance,
and directly carries Sandbox creation time, binding, raw status, lifecycle phase, preset, autosuspend
policy and blockers, plus the current incarnation Pod projection. Shared Kubernetes submessages
(`SandboxBinding`, `SandboxPod`, `SandboxPhase`, and `AutosuspendBlocker`) keep their existing shapes;
the response does not nest a second Sandbox representation.

`GetEnvironment` returns the current descriptor without waiting for a condition.
`WatchEnvironment` sends an initial `WatchEnvironmentResponse.environment` and subsequent complete
snapshots when descriptor facts change. Changes only to `observed_at` or unprojected Kubernetes
metadata do not emit a new snapshot. There is no readiness predicate or server-side readiness wait.

Observation depends on the control plane, not on a connection to the environment Runtime;
suspended targets remain observable. Target, execution-ServiceAccount, and namespace Pod watches are
established before reading the initial snapshot. Kubernetes collection resource versions bridge
list/watch setup; notifications trigger uncached reads and the same live UID/ownership checks as get.
Pod facts are included only when name, namespace, and UID match the Sandbox's currently reported
incarnation, so a stale same-name Pod is never returned as current.
An already admitted stream never follows a replacement target or execution account.

This is a current-state subscription, not retained transition history. Intermediate states may be
coalesced. Reconnecting starts with a current snapshot and has no cursor. Source closure, expiration,
or failure ends the RPC with `UNAVAILABLE`; a client must reconnect to refresh its observation.
Missing, replaced or no-longer-accessible targets end with `NOT_FOUND`; deletion does so for ordinary callers. Last observations
remain observations of their recorded time; silence or disconnection does not assert readiness.
Client cancellation ends the stream and all source watches. Server writes have a five-second
backpressure bound. A blocked write ends the RPC with `DEADLINE_EXCEEDED` and is not retried.

## Collection watch

`WatchEnvironments` begins with the caller-visible EnvironmentDescriptor set in bounded
`EnvironmentChunk.environments` snapshot chunks followed by `snapshot_complete`. A client replaces
its prior set only after completion, including for an
empty snapshot; it discards incomplete snapshots on disconnect. After the initial snapshot, `upsert`
replaces one complete EnvironmentDescriptor and `removed` removes the given namespace, name, and UID. A same-name
replacement removes the prior UID before upserting the new one. Visibility loss also removes an item.
Intermediate changes may be coalesced; changes to `observed_at` alone do not cause an upsert.
Reconnecting starts a fresh snapshot; there is no replay cursor or transition history. The assembled
view draws from multiple Kubernetes resources and does not
promise a globally atomic point-in-time read.

The service starts Sandbox, account, and Pod watches before reading the initial snapshot, and
rereads affected Sandboxes on notifications. Broad readers observe account changes because the
descriptor includes the current controller-owned execution account; ordinary collection watches
scope account events to the caller's same-name target. It indexes current incarnation Pod names,
including VM Pods; unrelated Pod events do not reread the inventory. Ordinary callers see only their exact live
owned target; explicitly granted direct readers see all targets, including creation and deletion
before or after the account exists. A missing or stale Pod is never substituted for the
controller-reported incarnation. Source loss ends the stream with `UNAVAILABLE`; the client
reconnects for a fresh snapshot. The admitted credential is reviewed before each frame and
periodically while idle, using the resolver's verdict cache. An expired or revoked credential ends
the stream; a successful review leaves it open. A stalled send is bounded by five seconds.

## Returned facts

- `target` is the Sandbox object's immutable identity; a runner Pod or VM incarnation UID is separate.
- `execution_account` is resolved from the live controller-owned ServiceAccount.
- `status` is the full current Sandbox status, and `pod` is the exact live incarnation Pod when one
  exists. Phase, readiness, operating mode, stop stage, wake setting, and runtime incarnation UID
  are derived convenience facts retained alongside the raw projection for existing callers.
- `observed_at` records when the descriptor was assembled and is not itself a watched fact.
- Explicit-resume eligibility includes a denial reason and is evaluated for the effective caller
  against its exact live ServiceAccount identity, the Sandbox `resumePolicy`, and applicable direct
  operator authority. It is independent of automatic wake-on-send policy. Deleting environments
  remain visible to broad readers without explicit-resume eligibility or a resume token.
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
account's same-name candidate and returns no cursor. A nonempty cursor is rejected for ordinary callers.
Broad readers can continue namespace pages; their grant permits seeing all namespace names.

Malformed requests and nonempty cursors from ordinary callers fail `INVALID_ARGUMENT`. Object authorization failures fail
`NOT_FOUND`. Kubernetes reads for the exact candidate or its live ownership fail closed with
`UNAVAILABLE` rather than returning partial facts.

## CLI

- `agentplane environments list [--limit N] [--after TOKEN]`
- `agentplane environments get NAMESPACE NAME UID`
- `agentplane environments resume NAMESPACE NAME UID --resume-token TOKEN`

All use the existing `AGENTPLANE_SESSIONS_URL` / `AGENTPLANE_SESSIONS_CREDENTIAL` connection. A
resume response is acceptance, not readiness, and an uncertain reply is never retried automatically.
Reconcile current readiness and any uncertain effect before opening a new Session or submitting
input. CLI and Action/MCP wait adapters over `WatchEnvironment` remain follow-up work.

## Resume and ownership

`ResumeEnvironment` uses the shared [lifecycle contract](../lifecycle/SPEC.md), including exact
ServiceAccount UID checks, policy opt-in, generation-bound preconditions, shared budget and unknown
outcome handling. List/get/watch remain read-only; resume permission is evaluated independently.
`REQUESTED` means the desired running state was accepted, not that the environment is ready. The
Sandbox Controller remains responsible for startup and readiness.
