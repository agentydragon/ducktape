# Agentplane Action Service

This package is the standalone canonical coordinator for ActionRequests. It owns its PostgreSQL
schema and `/v1/action-requests` lifecycle; the Agentplane integration app, Haku Console, BFFs, and
external harnesses remain clients rather than state owners.

The v0 executable seam is deliberately small:

- one invariant request envelope, with optional `origin` and `correlation` stored only as untrusted
  provenance;
- caller-own and operator-all reads: operator arguments are exact, while caller arguments and all execution result/error views recursively redact credential-shaped fields;
- a human operator Decision route, with expected-version and idempotency protection and one
  `decision_note` (optional, at most 2000 characters), shared unchanged with caller and operator;
  optional synchronous non-human `DecisionProvider`s run first and carry bounded
  `reason_code`/`reason_description` outcome evidence, with `decision_note=None`;
- automatic dispatch after allow, exactly one `Execution`, and no retry after dispatch may begin;
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
retried. The existing single-Execution claim and no-retry state machine are unchanged.

`runtime.running_executor` owns one typed `McpActionGroupExecutor` per reviewed group, using
`isinstance(McpExecutorBinding)` and the stdio or streamable-HTTP config below. It passes a group-keyed
executor mapping to the service and shares the same catalog objects with discovery. MCP `tools/list`
refreshes child Actions; execution rechecks the live schema. Echo is only an explicitly injected test
executor, never a production default or factory option.

An empty catalog starts with no offered actions. An explicitly configured missing/non-file YAML
path aborts startup rather than silently selecting that empty catalog. Missing bindings and unsupported kinds fail
settings validation without echoing input values; missing/invalid MCP config, connection failure, or failed initial
`tools/list` aborts startup before HTTP serving or pending-request recovery. All bindings are
validated before any server is launched. Startup unwinds already-opened adapters, including a
partially started adapter; shutdown stops service tasks before closing MCP clients/refresh tasks,
then Kubernetes and database resources. After startup, catalog-refresh failures retain the landed
adapter's unavailable-and-retry behavior. OAuth, credential/profile design, and a generic executor
registry are not part of this composition.

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

`operator_oidc` selects pinned RS256 JWT verification plus a mandatory issuer-scoped subject
allowlist. It is mutually exclusive with the legacy file-backed adapter; there is no fallback.
The destination records the actual token issuer and subject, not a shared BFF identity. Deployment
is still disabled until the explicit Authentik federation target is configured. See
[`../docs/operator_federation.md`](../docs/operator_federation.md) for settings and test evidence.
