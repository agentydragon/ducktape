# EnvironmentDiscovery SPEC

## Purpose

Expose bounded discovery of existing Agentplane Sandbox targets to the exact ServiceAccount that
executes them. This service returns facts needed to choose a target and prepare a Session request.
Its list/get methods are read-only. `GetEnvironment` may wait briefly for readiness without side
effects. Explicit lifecycle changes use the separate EnvironmentLifecycle service, which rechecks
current authorization and target state.

## Caller and object authorization

- Unary methods admit a direct TokenReview-verified ServiceAccount or an explicitly configured
  Actions delegation using the Runner Gateway's existing Dispatcher audience and served namespace
  set. Only `ListEnvironments`, `GetEnvironment`, and the separately authorized lifecycle
  `ResumeEnvironment` method are added to their exact method lists; this does not grant Dispatcher
  writes or Gateway proxy methods.
- The handler obtains the admitted effective caller from shared workload authentication and requires
  a complete ServiceAccount identity, including its UID. Direct calls retain their existing
  reviewed-account path.
- For each candidate, `sandboxreader.Reader.ExecutionAccount` rereads the exact live Sandbox UID and
  confirms its non-deleting ServiceAccount has the Sandbox's exact controlling owner reference.
- A result is returned only if namespace, account name, and account UID all match the effective caller.
- Get requires the complete Sandbox namespace/name/UID handle. Missing, replaced, deleting, or
  foreign targets are hidden as `NOT_FOUND`.
- The page request/response remains compatible with bounded pagination. Current Sandbox-owned
  accounts resolve by the account's same-name Sandbox candidate, so the service emits no cursor.
  It rejects nonempty unknown cursors and never forwards a namespace-wide Kubernetes token that
  can expose a filtered Sandbox key.

Delegated calls keep the Actions actor separate from the effective caller. They receive the same
live Sandbox-owned ServiceAccount UID checks as direct calls. The dispatcher still rechecks
identity at Session admission. Discovery is not an authorization lease and is not a substitute for
mutation-time checks.

## Returned facts

- `target` is the Sandbox object's immutable identity; a runner Pod or VM incarnation UID is separate.
- `execution_account` is resolved from the live controller-owned ServiceAccount.
- Readiness and operating state come from the current Sandbox status; `observed_at` records when the
  descriptor was assembled.
- `resume_eligibility` reports caller-specific explicit-resume permission and a reason. For ordinary
  callers, permission requires the live same ServiceAccount identity and Sandbox `resumePolicy`.
  This is independent of automatic wake-on-send policy.
- `wake_generation` and `resume_token` bind a resume precondition to the observed target UID and
  generation. The deterministic opaque token is not authority; EnvironmentLifecycle rechecks the
  effective caller, live identity, policy and state at mutation time.
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
account's same-name candidate and returns no cursor. A nonempty cursor is rejected as invalid; the
service does not expose foreign names or inventory counts through continuation state.

Malformed requests, wait bounds outside 0..30, and nonempty cursors fail `INVALID_ARGUMENT`. Object
authorization failures fail `NOT_FOUND`. Kubernetes reads for the exact candidate or its live
ownership fail closed with `UNAVAILABLE` rather than returning partial facts. A readiness wait
returns the latest descriptor when its bound expires; caller cancellation still ends the RPC.

## CLI

- `agentplane environments list [--limit N] [--after TOKEN]`
- `agentplane environments get NAMESPACE NAME UID [--wait-seconds N]`
- `agentplane environments resume NAMESPACE NAME UID --resume-token TOKEN`

All use the existing `AGENTPLANE_SESSIONS_URL` / `AGENTPLANE_SESSIONS_CREDENTIAL` connection. A
resume response is acceptance, not readiness, and an uncertain reply is never retried automatically.
Open or submit a new Session/input only after reconciling current readiness and any prior uncertain
effects.
