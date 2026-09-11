# Agentplane Action Service

This package is the standalone canonical coordinator for ActionRequests. It owns its PostgreSQL
schema and `/v1/action-requests` lifecycle; the Agentplane integration app, Haku Console, BFFs, and
external harnesses remain clients rather than state owners.

## External Connections

`identities: {personal: {enabled: true}}` configures static external authority names in the service
settings YAML. `connections.ConnectionAuthority` persists runtime named Connections and immutable
grant revisions in the existing database (migration `0008_external_connections`). The operator API
exposes `GET /v1/operator/identities`, list/detail at `/v1/operator/connections`, `PATCH` of a name
with `expected_version`, and `POST .../{id}/unbind` with `expected_version`.

Only the internal OAuth adapter may call `bind`, `activate`, `resolve`, or grant-specific `revoke`;
there is no HTTP endpoint accepting client-provided Identity/issuer/client/grant bindings. Bind is
idempotent by its consent grant UUID, reconnect locks the Connection and ends the old grant, and
activation is bounded by its deadline. The [app consent UI](../app/README.md) and
[OAuth adapter](#external-oauth) use this authority. An authenticated external adapter submits a resolved
`Grant.provenance()` through the trusted `ActionService.submit(..., external_grant=...)` keyword,
never a caller-envelope field. Admission stores the exact issuer/client/Connection/grant/revision
snapshot atomically with the first request. Shared-Identity idempotent retries preserve the original
snapshot, including after rename or reconnect. Existing workload requests retain a null snapshot.
External submissions initially require human approval; existing synchronous automatic providers
are not consulted for them.

`ActionStore` validates that snapshot against the original active grant and current configured
Identity under the Connection row lock, both during admission and before the dispatch claim. A
revoked, missing or disabled original authority prevents dispatch even if another grant now binds
the same Identity: the unstarted Execution fails with `external_grant_not_authorized`, retaining
the historical Decision. Already claimed work is not stopped. Receipt and executor projections carry
the original snapshot; no credentials are stored in it. Migration `0009_action_external_grant`
adds its nullable column without inventing provenance for pre-existing requests.

Action Service/PostgreSQL owns Connection authority; the integration app owns its operator UI.
Configurable policies and the Thread lifecycle are separate from this authority.

The v0 executable seam is deliberately small:

- one invariant request envelope, with optional `origin` and `correlation` stored only as untrusted
  provenance;
- caller-own and operator-all reads: operator arguments are exact, while caller arguments and all execution result/error views recursively redact credential-shaped fields;
- a human operator Decision route, with expected-version and idempotency protection and one
  `decision_note` (optional, at most 2000 characters), shared unchanged with caller and operator;
  optional synchronous non-human `DecisionProvider`s run first and carry bounded
  `reason_code`/`reason_description` outcome evidence, with `decision_note=None`;
- automatic dispatch after allow, exactly one `Execution`, and no retry after dispatch may begin;
- owner-only cancellation before the atomic dispatch claim, with no expected-version requirement;
- restart recovery: pending dispatches resume immediately; dispatching/running work is left alone
  until its own bounded lease expires, then becomes `execution_unknown` and may later be reconciled
  by an authenticated late completion or an authoritative status lookup — see
  [`../docs/executor_liveness.md`](../docs/executor_liveness.md);
- MCP adapters to reviewed upstream servers, with test-only injected executors in unit tests; and
- a durable, restart-surviving Action event sequence as the result-delivery surface: a caller polls
  `GET /v1/action-requests/{id}/events?after_sequence=<n>` from `decision_pending` to a terminal
  state, and every submit/Decision/dispatch/terminal/`execution_unknown` transition appends exactly
  one ordered event.

Migration `0006_decision_note` renames the existing human-note column without dropping data;
downgrade restores the old column name. Existing notes become caller-visible too. Notes are not
a secret channel: do not put credentials in them. Human decisions leave provider reason fields null.

## Consent transactions

`EnrollmentAuthority` accepts trusted, framework-validated authorization metadata from the OAuth
adapter. Only an opaque enrollment handle crosses to the integration-app URL; PostgreSQL stores
its hash. The operator-authenticated `/v1/operator/connection-enrollments/{handle}/preview` and
`/decision` routes bind the interaction to the app's server-side per-session browser secret and
the independently verified operator principal. The app enforces browser Origin/CSRF protection;
the service compares the browser-binding hash and operator on every decision.

Preview returns client presentation and expiry, not the held upstream URL or PKCE challenge.
Allow stores the configured Identity and either a new name or an explicitly confirmed
`ReconnectConnection` target/version, then releases only the stored framework
URL. Deny returns no redirect. Exact decision retries recover the original response; conflicting
or stale decisions cannot overwrite it. The configured Identity catalog supplies the UI picker.
The service validates the existing Connection version at decision and the binding authority checks
it again when reserving the replacement at code exchange. A concurrent rename, unbind, activation,
or competing reconnect requires fresh authorization rather than updating the reviewed version.
Migration `0011_enrollment_reconnect` retains the selected UUID/version alongside the consent.
Grant reservation revokes all prior active/pending grants atomically, even if later issuance fails;
the replacement stays pending until activation and neither old tokens nor Actions retarget.

The OAuth adapter calls `approved` with the framework code's client/redirect/PKCE tuple and the
verified, explicitly mapped upstream operator. It binds and validates the resulting pending grant,
then calls `claim_exchange` immediately before framework code consumption. Only one exchange can
claim an enrollment; dependency failures before the claim remain retryable, whereas ambiguous
post-claim failures require fresh OAuth. Activation remains the adapter's responsibility after
successful token persistence. Migration `0010_connection_enrollments` retains correlation
tombstones so expired codes cannot select a newer consent. These endpoints do not enable OAuth
or bypass Action review on their own.

## Decision providers

All configured synchronous providers run to completion; any deny dominates, otherwise any allow
wins, otherwise the request remains on the human-review path. Timeouts and exceptions contribute
`no_opinion` with bounded reason codes. Provider explanations are bounded audit evidence, not
unrestricted reasoning. Human and provider decisions commit through the same versioned,
idempotent Decision path; a losing provider callback cannot overwrite a winning human decision or
caller cancellation. Mandatory authorization bounds for future configurable policies are distinct
from these optional votes.

## Cancellation

`POST /v1/action-requests/{id}/cancel` takes no body or expected version and returns
`CancellationResult`: an `outcome` and the current caller-safe `request` receipt.
`ActionServiceClient.cancel(id)` uses this same route. Only the authenticated owning principal may
cancel; operator-all read/decision authority grants no override. Workload ownership is the Sandbox
principal, not a Thread or caller-supplied provenance. Unknown and non-owned requests both return 404.

| Request state                                 | Outcome             | Effect                                                         |
| --------------------------------------------- | ------------------- | -------------------------------------------------------------- |
| `decision_pending`                            | `cancelled`         | Cancel without creating a Decision or Execution.               |
| `allowed`, Execution `pending_dispatch`       | `cancelled`         | Preserve the Decision; mark the unstarted Execution cancelled. |
| `dispatching`, `running`, `execution_unknown` | `too_late`          | Leave unchanged; execution may already have started.           |
| `cancelled`                                   | `already_cancelled` | Idempotent success, no new event.                              |
| `denied`, `succeeded`, `failed`               | `already_finished`  | Return the unchanged receipt.                                  |

The request row lock serializes cancellation with Decision commits and the dispatch claim. A
successful cancellation guarantees no executor will run for that request, including after restart,
a queued dispatch task, or late approval/provider completion. Claiming is the cutoff even before
the executor is invoked. Cancellation does not signal executors or kill processes; disconnecting
or cancelling an HTTP/MCP wait does not cancel the underlying ActionRequest.

A successful cancellation appends one canonical `cancelled` event with authenticated
`actor_principal` and timestamp. Migration `0007_cancellation_actor` adds the nullable actor field;
existing and non-caller-cancellation events leave it null. An unclaimed Execution retains null
`started_at`, result and error, with `completed_at` set to the cancellation time. Repeating the
original submission idempotency key returns the same cancelled request; an intentional new attempt
needs a new key.

## Delivery: durable events and bounded waits

The durable Action event sequence (`action_event`, exposed at `.../events`) is the first-slice
result-delivery surface. `after_sequence` is the last sequence number the caller already holds;
polling with it is a cheap, idempotent no-op once no new events exist, so a caller can safely poll
from submission to a terminal state without missing or duplicating a transition.

An earlier `action_outbox` table recorded a pending-decision delivery reference for a future push
notifier. Nothing ever drained it — no consumer was wired into `main.py` — and it duplicated data
already in `action_event`/`action_request`, so it has been dropped (migration
`0004_drop_action_outbox`). The `.../events` polling surface above is not a prerequisite on the
later Event & Notification Hub, which is expected to consume the Action event sequence directly
rather than an outbox.

`waits.ActionWaiter` provides bounded, notification-driven receipt reads for transports that offer
waiting. `WaitOptions` defaults to immediate reads and caps waits at 30 seconds, with `decision`
and `terminal` predicates. Each ORM insertion of a canonical Action event emits a UUID-only
PostgreSQL `NOTIFY` in the same transaction. `updates.ActionUpdates` owns one dedicated listener
connection per service instance and coalesces wakeups per waiting request. It subscribes before
rechecking durable state and releases registrations on every exit path. A lost listener fails
bounded waits explicitly until the listener is restarted; it never falls back to timed queries.
The consumer owns listener startup/shutdown, separate from the dispatch coordinator.

## Generic MCP frontend

The same service process serves stateless Streamable HTTP at `/mcp`. The FastAPI lifespan starts
the PostgreSQL update listener and MCP transport and unwinds both on shutdown/startup failure.
This is the production `main.py` composition, not a sidecar, upstream-tool proxy, or second store.
Requests use the same Sandbox bearer/egress placeholder substitution as the REST workload API.
Operator/OIDC bearers remain confined to `/v1/operator/...`; configured external OAuth grants are
also accepted by `/mcp`. No public ingress or harness deployment is added here. FastMCP's automatic Host/Origin
guard protects loopback access without categorically rejecting requests carrying Origin; authority
comes from the explicit validated bearer, not Origin or browser cookies. Browser CORS policy can
be configured alongside future external exposure.
Staging's current `egresspolicy-agentplane-actions.yaml` permits only the REST paths; deploying
Sandbox MCP clients also requires an explicit `/mcp` egress allowance with the same workload
credential substitution. The protocol tests exercise substitution at that boundary, not a claim
that the current cluster policy already permits the new route.

### External OAuth

The optional `oauth` settings enable FastMCP 3.4.4's DCR, discovery, authorization, callback,
token and revocation routes in this process. `ActionsOAuthProxy` holds the validated upstream
redirect in the durable enrollment authority and sends the browser to the integration app's
consent page. The page chooses a new name or existing Connection and configured Identity; raw client
registration and upstream login alone create no caller authority.

After consent, the adapter verifies the upstream issuer/subject against the explicitly configured
single-operator mapping, validates the pending binding, and atomically claims the enrollment
before FastMCP consumes its code. Only one token family may issue per enrollment. Failures before
the claim can be retried; an ambiguous failure after it requires fresh OAuth, not another issuance.
Tokens contain an opaque grant reference. Every bearer admission and refresh resolves the current
canonical grant; unbind/revocation cannot silently retarget an old token to a new Identity.
The local revocation endpoint ends the canonical grant independently of upstream IdP revocation;
it does not forward local credentials upstream or revoke an upstream account. Encrypted SDK
metadata remains bounded by its existing TTL after the grant is ended.

Configure dedicated upstream client credentials, discovery/issuer pins, public `base_url`,
`integration_app_url`, and the exact `approving_operator` issuer/subject mapping. Provider-scoped
subjects are never assumed equal. `jwt_signing_key_file` supplies a stable key across replicas;
`encryption_key_file` supplies a Fernet key for credential-bearing PostgreSQL KV in the same Actions
database (`agentplane_oauth_kv`). Neither key is generated at startup. Runtime settings add no
deployment, Authentik client, ingress, or browser CORS policy automatically.

External MCP callers share receipt ownership/idempotency within the configured Identity while
each Action retains immutable submitting Connection/grant/revision/issuer/client provenance.
Production admission and dispatch use the same Connection authority. Sandbox bearers still use
live workload validation and egress substitution; OAuth does not grant an operator bearer bypass.

| Tool                         | Use                                                                                                                                               |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_actions`               | Compact `{group, name, available}` entries; optional group filter, `limit` (default 30, max 100), keyset `after`/`next_after`.                    |
| `get_action`                 | One definition by group/name. `include_fields` on either catalog read accepts only `input_schema` and `description`; omitted/empty excludes both. |
| `request_action`             | The existing request envelope under `request`; caller-scoped idempotency and input validation are unchanged.                                      |
| `get_action_request`         | One own-caller receipt by `request_id`, not an Action definition.                                                                                 |
| `cancel_action_request`      | Own-caller pre-claim cancellation by request ID, without a version; returns canonical outcome and receipt.                                        |
| `list_action_request_events` | One own-caller event page; `after_sequence`, `limit`, optional `next_after_sequence`.                                                             |

`cancel_action_request(request_id)` explicitly withdraws an own-caller request before dispatch
claim, without a version parameter. It returns the canonical outcome (`cancelled`,
`already_cancelled`, `already_finished`, or `too_late`) and receipt; it never interrupts an
executor. Retry the original submission key to recover that same cancelled receipt. It is
independent of cancelling or disconnecting a wait.

Both submission and receipt reads accept `wait_seconds` (0–30, default 0) and `wait_until`
(`decision` or `terminal`, default terminal). Waits use commit notifications rather than periodic
queries. A deadline returns a receipt, not a cancellation. On an ambiguous response, reuse the
original request/key; transport or notification failure must not prompt a new Action. Workload
authorization is revalidated after a bounded wait, before returning data. The generic MCP tool
schemas never expand the dynamic Action catalog, and no Action output-schema metadata is added.

Workflow: list identifiers, fetch one input schema if needed, submit once, then wait/read the
request ID or resume events. Discovery/read failures cannot submit anything; a wrong caller sees
the same not-found response as an absent request. Discovery projects no executor configuration,
group descriptions, or hidden schemas through nested payloads.

## Action catalog

`catalog.ActionCatalog` is the Agent-facing discovery seam: an `ActionGroup` (e.g. `github`) is the
executor/backend ownership unit, and each child `Action` (e.g. `get_file`) is namespaced under it as
the pair `(github, get_file)`. `GET /v1/action-groups` lists every configured group with its Actions'
descriptions and input schemas; `GET /v1/action-groups/{group}/actions/{action}` looks up one Action
directly and 404s clearly on an unknown group or action. Both are workload-authenticated reads with
no owner-scoping, since the catalog is the same for every caller.

Group bindings are reviewed runtime configuration, not a dynamic registry: `main.Settings.action_groups`
follows the same `AGENTPLANE_ACTIONS_CONFIG_FILE`-mounted-YAML convention as the integration app's
`AGENTPLANE_CONFIG_FILE` (`x/agentplane/app/main.py`), so an operator edits the group configuration and the
process picks it up on restart — sufficient because ActionGroup/executor bindings change at
operator/deploy cadence, not per-request, and the app uses a `Recreate`-strategy Deployment.
`McpExecutorBinding.config` is never exposed by discovery; only the human-authored executor description is.

The catalog is also the admission and routing authority: `ActionService` resolves the submitted
structured `action: {group, name}`, rejects unknown or unavailable groups/Actions and unbound groups before persistence,
and validates arguments against the advertised JSON Schema before persistence or provider
evaluation. Invalid arguments return HTTP 422 without reserving the idempotency key.
Dispatch uses the executor bound to that group. `ActionStore` owns persistence and lifecycle,
not a second admission registry. Executors expose execution only, not an action registry.
Dispatch resolves the identity again, so a removed action is terminally refused rather than rerouted or
retried. Each service replica may own its own current FastMCP connection; the database-backed
single-Execution claim and lease token, not the MCP connection, fence dispatch and keep a lost
attempt from being replayed. The existing no-retry state machine is unchanged.

`runtime.running_executor` owns one typed `McpActionGroupExecutor` per reviewed group, using
`isinstance(McpExecutorBinding)` and the stdio or streamable-HTTP config below. It passes a group-keyed
executor mapping to the service and shares the same catalog objects with discovery. MCP `tools/list`
refreshes child Actions; execution rechecks the live schema. Echo is only an explicitly injected test
executor, never a production default or factory option.

An empty catalog starts with no offered actions. An explicitly configured missing/non-file YAML
path aborts startup rather than silently selecting that empty catalog. Missing bindings and unsupported kinds fail
settings validation without echoing input values. All bindings are validated before any server is
launched. Credentialless MCP groups must connect and complete initial `tools/list` before HTTP
serving or pending-request recovery; a failure aborts startup. OAuth-linked groups are different:
an unlinked or expired provider starts unavailable, and its supervisor connects only after the
shared linkage authority reports a current link. It keeps the group unavailable across connection,
catalog, and authorization failures and creates a fresh FastMCP client for the next connection.
The HTTP auth hook resolves the current linkage token for every request, so ordinary token refresh
does not require restarting the service. Startup unwinds already-opened adapters, including a
partially started adapter; shutdown stops service tasks before closing MCP clients/refresh tasks,
then Kubernetes and database resources. After startup, catalog-refresh failures retain the landed
adapter's unavailable-and-retry behavior.

Requests, execution payloads and durable rows use `action: {"group": "everything", "name": "echo"}`.
The fields remain separate throughout discovery, validation and dispatch; no concatenated identity
or legacy name is accepted. Migration `0005_structured_action` removes the unused string column
without inventing a compatibility mapping. Adding the required replacement column fails
transactionally if unexpected preexisting rows exist.

## Credentialless upstream echo auto-allow

`fixture_auto_allow` defaults to absent. Staging opts in for the reviewed `everything` group,
bound to the existing upstream image described in [the deployment note](../docs/mcp_fixture_choice.md).
The provider allows only that group's `echo` Action with exactly one string `message` argument
of at most 200 characters, from an authenticated in-scope Kubernetes Sandbox UID. Unavailable
or missing discovery, other tools, extra arguments, and untrusted identities get no allow vote.
The MCP adapter still checks the current backend schema. Deny dominance and human fallback
remain unchanged; opting in does not enable the operator API or grant other upstream tools.
No custom MCP server or image is built. The real staging test is
`//x/agentplane/acceptance:test_mcp`: it tasks real agents with discovery, submission, event/result
polling and a JSON report checked against the upstream echo result.

## Authentication boundaries

`allowed_service_account_namespaces` lists the Kubernetes namespaces whose ServiceAccounts
may authenticate sandbox callers. It does not approve Actions or select an MCP destination.

Sandbox calls use ordinary `Authorization: Bearer <workload token>` at this service. The runner does
not hold that token: it presents the public
`agentplane-credential-agentplane-workload` placeholder to the existing pod-local/central egress
path, whose generic `authenticatedWorkloadToken` source substitutes the already-authenticated
`agentplane-egress` bearer for the exact first-party destination rule.

At the destination, `SandboxPrincipalAuthenticator` and `SandboxPrincipalResolver` from
`//x/agentplane/sandbox_auth` perform TokenReview plus live Pod/Sandbox-owner resolution. Ownership
is derived only from the resolved Sandbox namespace and UID. ServiceAccount subject lists, identity
headers, and request `origin`/`correlation` fields are never authorization. Thread and Agent fields
remain untrusted provenance until an authoritative binding exists; workload authentication performs
no Thread or Agent lookup.

Operator/BFF calls use the separate `/v1/operator/...` surface and a separate replaceable
`OperatorAuthenticator`. The production composition is fail-closed unless explicitly configured.
Its minimal v0 file-backed bearer adapter retains only a digest and is not a claim that static
Kubernetes ServiceAccount lists are the final operator design.

Migrations run separately through `:migrate`; the server verifies the migrated schema and never
creates tables at startup. `:image` and `:migration_image` are separate OCI targets. The staging
manifests give the service its own PostgreSQL cluster and credentials rather than coupling it to the
integration app database.

## MCP executor transports

`McpActionGroupExecutor.from_group` owns one persistent MCP connection for a group. Its
`McpExecutorBinding.config` accepts a stdio launch (`command`, optional `args`, `env`, `cwd`, and
`transport: stdio`) or a credentialless streamable-HTTP endpoint:

```yaml
transport: streamable-http
url: http://127.0.0.1:8000/mcp
```

HTTP uses the pinned FastMCP `StreamableHttpTransport` and MCP session implementation, including
JSON/SSE responses and session shutdown. Both transports use the same catalog refresh, live schema
validation, safe tool-error mapping, and ambiguous-call failure path; a failed `tools/call` transport
exchange is not retried. HTTP config rejects userinfo, URL queries/fragments, launch fields, and authentication
or header settings. The production composition uses this same transport selection. OAuth and
credential profiles are outside this seam. Invalid discovered schemas or duplicate supported tool names
make the entire group unavailable, clearing stale Actions. Invalid live schemas are refused before
`tools/call`. Production startup and shutdown report only the group and failure category, not raw
transport exceptions or endpoint values.

### OIDC operator adapter

`operator_oidc` selects pinned RS256 JWT verification for the Action audience. Authentik's target
provider policy governs who can obtain that audience; the service does not maintain a second subject
allowlist. It is mutually exclusive with the legacy file-backed adapter; there is no fallback. The
destination records the actual token issuer and subject, not a shared BFF identity. Deployment is
still disabled until the explicit Authentik federation target is configured. See
[`../docs/operator_federation.md`](../docs/operator_federation.md) for settings and test evidence.

## Action live updates and approval Web Push

The operator SSE endpoint `/v1/operator/action-requests/stream` subscribes before reading a
snapshot, then wakes on PostgreSQL Action commit notifications. Each replica has its own LISTEN
connection; none depends on the process which accepted the write. Notification loss terminates
streams explicitly. Reconnection obtains an authoritative snapshot rather than treating NOTIFY
as a durable log. Operator bearer authorization is rechecked on updates and keepalives.

The optional `web_push` configuration enables browser subscription storage and background delivery:

- `private_key_pem`: stable VAPID private key, supplied through deployment secret management;
- `subject`: VAPID contact URI;
- `public_base_url`: integration-app origin used for Action links;
- `allowed_push_hosts`: exact reviewed HTTPS browser push-service hostnames.

No push configuration is enabled by this code change. Browser subscriptions are registered through
operator-authenticated `/v1/operator/push/subscriptions`; the integration app forwards its browser
management requests through federation, so Authentik controls access at registration and on later
operator API requests. Stored subscriptions are notification-only and do not confer decision
authority. Background delivery has no operator bearer with which to recheck Authentik policy, so
revoking an account does not retroactively delete an existing subscription; remove it on the next
authenticated management request or through the subscription store. The sender rejects other endpoint
hosts, credentials in URLs, non-HTTPS ports, and redirects; deployed network policy must also
constrain push-service access. VAPID rotation requires browser resubscription.

Delivery uses committed Action state, PostgreSQL NOTIFY wakeups, and per-browser delivery records.
A short subscription-row lock serializes network sends from different replicas without holding an
Action row lock. Successful send acknowledgement advances the delivery record; transient failure
leaves it retryable. Startup/reconnect and a 30-second background recovery sweep find missed work.
The open Actions page does not poll. Browser deletion cascades its delivery records. A crash after
send but before commit may resend; stable notification tags/topics collapse repeats. This is not
exactly-once delivery and a push-service acknowledgement is not proof of device display.

Only pending requests need an actionable notification. Requests resolved before reconciliation
need no new alert; browsers previously notified receive a non-actionable resolution notice. Push
payloads contain Action identity/version, not arguments, credentials, or results. Notification
buttons use the integration app's ordinary operator session and canonical Decision contract.

## Shutdown budgets

SIGTERM/SIGINT synchronously fence dispatch and HTTP admission. `/readyz` becomes unavailable;
`/healthz` remains a liveness check. Uvicorn waits at most 5 seconds for HTTP requests (including
SSE); this time counts against the 20-second execution drain. Forced cancellation gets another
5 seconds to record unknown outcomes. Both deployed environments allow 60 seconds for termination,
leaving a margin for transport/database cleanup. No preStop delay consumes that budget.

MCP renewals run every third of the granted lease duration, with each renewal RPC bounded by the
same interval. The entire execution exchange has a 10-minute deadline. Cancellation joins the
local exchange and renewal tasks; it cannot establish that a remote effect was cancelled.
