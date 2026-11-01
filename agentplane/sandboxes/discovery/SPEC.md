# EnvironmentDiscovery SPEC

## Purpose

Expose bounded, read-only discovery of existing Agentplane Sandbox targets to the exact ServiceAccount
that executes them. This service returns facts needed to choose a target and prepare a Session
request. It does not create environments, attach Sessions, control lifecycle, or grant authorization.

## Caller and object authorization

- Unary methods admit a direct TokenReview-verified ServiceAccount or an explicitly configured
  Actions delegation using the Runner Gateway's existing Dispatcher audience and served namespace
  set. Only `ListEnvironments` and `GetEnvironment` may be added to the Gateway's exact delegated
  method list; the grant does not extend to Dispatcher writes or Gateway proxy methods.
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
- Resume eligibility reports only the existing `wakeOnSend` plus configured wake-caller policy.
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

Malformed requests and nonempty cursors fail `INVALID_ARGUMENT`. Object authorization failures fail
`NOT_FOUND`. Kubernetes reads for the exact candidate or its live ownership fail closed with
`UNAVAILABLE` rather than returning partial facts.

## CLI

- `agentplane environments list [--limit N] [--after TOKEN]`
- `agentplane environments get NAMESPACE NAME UID`

Both use the existing `AGENTPLANE_SESSIONS_URL` / `AGENTPLANE_SESSIONS_CREDENTIAL` connection. The
CLI is a reader only; opening and submitting Sessions remain separate operations.
