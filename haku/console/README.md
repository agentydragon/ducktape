# haku/console — Haku's interactive console

A small FastAPI service that serves the Haku console as a **React single-page app
over a JSON API**. It is the trusted operator interface: Authentik operator-only,
reviewed ducktape code. It replaced the static nginx + git-sync dashboard (now retired).

All product surfaces have **moved** to haku-state's `ui/` — Haku's own UI service
(`haku-ui`), which the console frames **full-page** as a sandboxed cross-origin iframe. The
console is now just the trusted outer shell: the capability tier (launch-routine) plus the
bridge that brokers the iframe's privileged requests. It holds no haku-state write credential.

**Trust boundary:** the console is reviewed/released ducktape code, so it runs in
its **own `haku-console` namespace** — deliberately _not_ `haku-sandbox`, the
namespace Haku has full CRUD over. Haku therefore has no RBAC to read the console's
secrets/logs or patch it, and the console sits outside the `haku-egress-proxy` egress
fence (that fence keys on `haku-sandbox`). This is the confidentiality boundary that
lets the console hold secrets Haku may not read (e.g. the Claude Code web session
bearer). Haku's full security model
(threat model, enforcement inventory, invariants): <../docs/security.md>.

## The capability tier — privileged actions, operator-gated

The launch-routine path remains in the **capability tier**
(`capabilities.py`, `/api/capabilities/*`). It uses console-only secrets and acts on the
world, so it's gated hard (see <../docs/security.md> → enforcement inventory #11):

- **Exact-Origin gated.** Every browser mutation must carry an `Origin` exactly equal to
  the canonical console origin, so the same-site but untrusted Haku iframe cannot ride the
  operator's Authentik session cookie to fire a capability.
- **Server-side secret.** The bearer is read from `Settings` / the
  `haku-routine-launch-token` secret and attached to the upstream call; it never
  reaches the client.
- **Audited.** Every invocation logs to stdout in the `haku-console` namespace, which
  Haku has no RBAC to read.
- **Tiny, PR-gated allowlist.** Today one capability: `POST /api/capabilities/launch-routine`
  fires the Haku claude-code-web routine via its public Anthropic fire URL, optionally with
  per-run `text`. Adding a verb is a ducktape PR, never runtime data.

Firing must be a genuine operator gesture against **trusted-rendered chrome**: the
agent-authored iframe can only _request_ a launch (`requestLaunch` over the bridge); the
shell renders its own confirm (showing the prompt verbatim) and only then fires. So agent
UI can ask for the capability but can never script or spoof it.

**Migration (in progress):** launch is moving off this bespoke tier onto the standard MCP
approval queue — the `haku_routine` in-process MCP server's `launch_routine` tool (below)
fires the same routine, gated by the ordinary approvals panel instead of a separate confirm.
This capability path stays only until haku-ui submits `launch_routine` through its backend and
the `requestLaunch` bridge verb is dropped, at which point `capabilities.py` retires entirely.

There is **no** low-privilege "trace" write tier anymore — operator feedback now writes
straight into haku-state from haku-ui (which Haku already owns), so the console needs no
haku-state git credential or clone at all.

## MCP approval queue — authored tool calls, console-approved

The console also owns the privileged MCP-tool escape hatch. `ToolCallApplicationService` in
`tool_call_service.py` is the actor-scoped lifecycle boundary. Agent callers, including Haku and
haku-ui, enter only through the FastMCP adapter in `mcp_server.py`; the HTTP routes in
`mcp_approval.py` are the operator browser's audit/approval adapter. The service mints the canonical
`tool_call_id`, records the audit entry, runs the reviewed
auto-approval decision, asks the trusted console frontend for approval when it does not match,
executes the MCP tool, and keeps the result. The decision validates arguments
against the existing FastMCP tool's generated schema; its audit-safe evaluation string is recorded
even when the call stays manual. A call whose arguments fail an owned **in-process** schema is
persisted **born-denied** — the validation error returns to the MCP caller immediately and the call
never enters the approval queue (it can never execute, so it must not consume operator attention);
console-side lookup/schema errors still fail closed to manual review. haku-state stores only authored requests
(`tool_requests/*.yaml`) and UI affordances (`<tool-call request="...">`); there is no
`tool_results/` mirror.

Core HTTP mutation/audit endpoints and MCP reflection tools:

- MCP `list_mcp_servers` and `get_mcp_server_status` — the shared Agent/console reflection path for
  configured servers, linked accounts, and current tool availability. The list is passive;
  the targeted status call actively refreshes credentials and contacts the server. Entries are
  explicitly `alive` or `degraded`, with the failure stage and reason preserved as data.
- `POST /api/mcp/operator-auth/{server_id}/connect`,
  `DELETE /api/mcp/operator-auth/{server_id}`, and `GET /api/mcp/operator-auth/callback` —
  operator account association for MCP servers whose config sets `auth: {kind: remote_server_oauth}`
  (this flow lives in `mcp_operator_oauth.py`, not the approval router). The catalog stays in the console
  YAML/ConfigMap; Postgres stores only short-lived DCR/PKCE flow state and per-operator token
  associations.
  Connecting is available only while unconnected; disconnect first to replace an account link.
  Client acquisition uses the `client_registration` discriminator. `dynamic` performs DCR and owns
  `client_name`; `preregistered` skips DCR and owns the deploy-provisioned `client_id`.
- `POST /api/operator-connections/{connection}/connect`,
  `DELETE /api/operator-connections/{connection}`, and `GET /api/provider-connections/callback` —
  deploy-named per-Operator connections to well-known external OAuth providers for in-process
  servers (this flow lives in `provider_connection.py`). Each connection owns its display name and
  scopes and selects a deploy-named provider instance. Provider instances of the same protocol may
  use different fixed pre-registered clients. Postgres stores and self-refreshes one grant per
  `(operator_id, connection_name)` and records which provider instance issued it. A
  replica-coordinated background sweep refreshes expiring remote-server, provider, and (when
  hostexec is enabled) Operator-login grants even when no foreground tool call is using them.
- `POST /api/oauth-results/{result_id}` — consumes the short-lived, Operator-bound result created by
  either account-link callback. Provider callbacks redirect to `/_console/oauth-result/{result_id}`;
  MCP callbacks return to `/_console/settings` and announce the consumed result there. Result text
  is never carried in either URL. See
  [OAuth browser surfaces](docs/oauth_browser_surfaces.md) for the renderer boundary and remaining
  backend-page consolidation.
- `GET /api/approvals/pending` and `WebSocket /api/events/ws` — canonical-Operator-routed
  frontend state plus lossy invalidations. REST remains the source of truth; the WebSocket only
  wakes the shell to refresh.
- `POST /api/tool-calls/{tool_call_id}/decision` — exact-Origin-gated trusted-frontend approval/denial.
- `GET /api/tool-calls` / `GET /api/tool-calls/{tool_call_id}` — operator-only audit/result reads.
  The list endpoint accepts repeated `status` filters and a datetime `since` filter on `updated_at`.
- MCP `list_node_daemons` — shared Agent/console heartbeat-derived state for configured execution
  daemons (`connected`, `busy`, `stale`, or `offline`). The Settings panel refreshes it every ten
  seconds through MCP. The separately authenticated `/api/node-daemons/v1/*` machine API lets daemons
  heartbeat, long-poll for durable Postgres-backed work, renew leases, and submit idempotent
  results; it is intentionally outside browser Operator auth.

The browser REST API requires the operator's Authentik session, and mutations additionally require
the console's exact `Origin`. `/mcp` accepts either an Agent bearer or that same DB-revalidated
Operator session; browser MCP requests also require the console's exact `Origin`. The
approvals panel renders in trusted
console chrome, not inside Haku's iframe, and does not block the framed Haku UI. If a server's `auth`
is `remote_server_oauth`, approval execution
uses the approving operator's linked OAuth token and refuses to move the call out of
`pending_approval` until that association exists. A `static_bearer` credential can be configured on a
different server for reflection or fallback wiring, but it is not silently substituted for
operator-approved execution on a `remote_server_oauth` server.

### MCP server (`/mcp`)

`mcp_server.py` mounts one native MCP server at `/mcp` for both authenticated principals. A connected
Claude client (the claude.ai custom connector / the `claude` CLI / haku-ui backend) enters as an
Agent and calls connected-server tools through `ToolCallApplicationService.submit_and_wait`. The
trusted console frontend enters as its current Operator and calls the same tools through
`execute_direct`, resolving downstream authentication in that Operator's context (an
operator-linked OAuth token where configured, otherwise the server's configured credential).
Direct Operator calls do not create tool-call rows, approval events, or promises; they exist so
trusted renderers can resolve reference data through the real MCP surface instead of bespoke HTTP
fetchers.

Discovery is request-local: either principal sees remote servers connected by its canonical
Operator, plus shared configured/in-process servers. For Agents, that surface is divided into two
buckets:
tools the policy **unconditionally**
auto-approves (Gmail and Google Calendar reads, read-only grocy-sf, tana's read tools —
`search_nodes`, `read_node`, `get_children`, `open_node`, `list_tags`, `list_workspaces`,
`get_tag_schema`, plus the idempotent `get_or_create_calendar_node`, and postscanmail-mcp
reads) appear as
transparent **pass-throughs** (original schema, real result); everything else keeps the same
`<server>__<tool>` name but uses an envelope `{input, title?, rationale, wait_for_approval_ms?}` that
returns the real result if approved within the wait, else a **promise** (a pending `tool_call_id` +
an operator-facing deep-link `url`) the agent resolves via the `get_tool_call` / `list_tool_calls`
read tools. `list_mcp_servers` passively reports the
configured catalog plus each persisted per-Operator OAuth/provider status object. Each server's
discriminated `backend` object mirrors the safe configuration shape (`remote_mcp` with URL/auth or
`in_process` with credential kind); static-bearer secret references are deliberately omitted. These status objects
match the console's existing non-secret status structures, including `status`, `connected_at`,
`token_expires_at`, `scope`, and the safe display identity; access tokens, refresh tokens, and client
secrets are never included. A cataloged provider connection whose deploy-time OAuth client is absent
reports `status: unprovisioned` instead of disappearing; the Settings panel renders that state with no
Connect action. Authentication kinds without a separate operator-linked connection report
`connection: null`. The tool never refreshes credentials or contacts downstream servers. Live tool
discovery remains the normal MCP `tools/list` path. `get_mcp_server_status(server_id)` is the active
counterpart: it reflects one configured server now, so it may refresh the operator's credentials and
contact the downstream server. By default it returns tool names, descriptions, and annotations but
omits the potentially large input schemas; callers opt in with `include_tool_schemas=true`. Credential
resolution and downstream discovery failures return distinct degraded stages and reasons rather than
failing the reflection call; direct calls in a known
`<server>__<tool>` namespace return the same actionable error instead of appearing as an unknown tool.
The promise-semantics preamble lives in each tool's
**description** (many MCP clients, claude.ai included, never surface a server's `instructions`). Auth is a Haku-owned
`HakuAgentOAuthProxy` composed with the configured `static_agents` through
`HakuFailurePreservingMultiAuth`. An explicit `Authorization` header always selects Agent admission;
an invalid bearer never falls back to an ambient browser cookie. FastMCP still owns DCR, PKCE,
callback, code, and token-family
machinery; Haku adds the Operator-authenticated Agent-enrollment ceremony and resolves both OAuth
and static credentials through the same canonical Agent authority. The `/mcp` surface only
submits/reads — there is no decision tool — so an OAuth caller cannot self-approve; approval stays
in trusted console chrome. Approved calls execute against the console's stored Operator credentials,
so an incoming token's blast radius is "call the console's submit/read tools" and nothing else.
OAuth state is required to use the console's Postgres; Haku never falls back to FastMCP's
process-local store or Valkey.

The trusted frontend validates the console-native reflection results with the same generated MCP
result-schema catalog used by Gmail, Google Calendar, Grocy, and routine renderers. The catalog is
generated from the Python response models at build time; the frontend does not restate those wire
models in handwritten Zod.

### Canonical Agent authority and enrollment

Alembic revision `0010` is the single forward-only database baseline. It directly installs one
Postgres graph shared by interactive OAuth and configured static Agents:

```text
Operator -> IdentityAnchor -> OidcIdentity
Operator -> Agent -> AgentNameReservation
Agent -> CredentialBinding -> AuthorizationGrant | StaticCredential
ToolCallPrincipal -> exactly one of operator_id | binding_id
```

The durable contract is:

- `Operator`, `Agent`, and every authority-bearing relationship use local immutable UUIDs. An exact
  verified `(issuer, subject)` identifies an OIDC identity; a configured Authentik trust-domain
  anchor is the only path by which identities at the browser and MCP issuers converge on one
  Operator. Username is presentation only.
- OAuth `client_id` describes client software/registration metadata. It is never the Agent,
  Operator, grant, or credential binding, and unauthenticated DCR does not create an Agent.
- Every Agent has a required normalized non-empty display name, globally unique by its normalized
  key. The name is presentation owned by the canonical Agent; it is never a credential or durable
  identity key.
- A `CredentialBinding` owns credential kind, generation, predecessor, and lifecycle. An OAuth
  `AuthorizationGrant` owns client software, authorizing identity, scopes, and token-family
  evidence. Static rotation and OAuth reconnect create successor bindings instead of mutating
  Agent identity.
- An Agent-originated tool call persists only its exact binding provenance. Agent, owning Operator,
  and display name derive through canonical joins, and approval/execution revalidate that binding
  so queued work cannot transfer to a replacement credential.

Interactive enrollment proceeds as follows:

1. FastMCP validates client, redirect, resource, scopes, and S256 PKCE through its public
   `authorize()` path.
2. Haku reserves the exact public client/redirect/challenge tuple only as a temporal collision key
   and creates a random `EnrollmentInteraction`.
3. The browser independently logs into Haku through Authentik. Opening the page binds its verified
   identity and canonical Operator to the interaction exactly once.
4. The Operator explicitly denies or allows a required name for a new Agent or reconnects an
   existing owned Agent. This records intent but issues no grant yet.
5. FastMCP performs its untouched Authentik callback and creates the downstream code.
6. At exchange, Haku verifies the MCP-side access-token principal and requires the same active
   canonical Operator as the browser interaction. One locked transition creates the draft Agent
   when needed plus its issuing binding and grant.
7. FastMCP carries only opaque `grant_id` context through the token family. The first successfully
   verified MCP tool request atomically activates the Agent and binding.
8. Access load, transparent/explicit refresh, decisions, and execution revalidate the grant,
   binding, Agent, and Operator. Local revoke remains authoritative even if best-effort upstream or
   individual-token cleanup fails.

The enrollment cookie is only a short-lived page/CSRF binding: path-scoped, `HttpOnly`,
`SameSite=Lax`, and `Secure` in production. It contains no name, token, raw claim, or durable
authority. The trusted Console SPA receives an escaped typed view model from same-origin APIs;
decision endpoints enforce the browser binding and exact Origin before changing authority state.

The runtime caller is `OperatorActor | AgentActor`. Only the authority constructs an
`AgentActor(agent_id, operator_id, binding_id)`. Agents can submit/read only their own calls;
Operators can read/decide all and only calls they own; Agents never approve themselves. Repository
operations have no unscoped or `None` actor mode.

Haku supports one exact FastMCP version at a time. The adapter's sole private seam is `_code_store`
read/delete during code exchange; protected claim, scope-translation, and transparent-refresh hooks
are version-pinned. It does not replace route construction, registration, transaction storage,
callback, PKCE, or token issuance. Adapter compatibility and mounted enrollment/token/refresh/
revocation tests are mandatory before a repin.

Client-side credential deletion is generally invisible to Haku. `last_seen_at` is observation, not
connection state; an Operator-owned revoke/disable action is the authoritative product control.
Postgres, Valkey for generic consumers, Kubernetes Secrets, and their backups/admin readers are the
accepted private credential boundary. Extra application encryption is optional defense-in-depth,
while tokens, codes, secrets, callback queries, and raw OAuth forms must never enter logs or API
models.

### In-process MCP servers — no second deployment

An `mcp.servers` entry explicitly selects either a `remote_mcp` backend with an HTTP URL and
transport `auth`, or an `in_process` backend with an implementation `credential`. The latter is a
registered **in-process `FastMCP` instance**: `fastmcp.client.Client` accepts a `FastMCP` object
directly (an in-memory `FastMCPTransport`), so `McpServerClient` runs the exact
same `Client(...)` calls either way. The application service still owns approval/audit and the HTTP
adapter still owns exact-Origin admission, with the same live `tools/list` reflection.

An in-process credential is consumed by reviewed implementation code only during execution; it is
never passed to FastMCP as transport authentication. The caller's Agent bearer authenticates the
outer `/mcp` request and resolves the acting Operator; it is never a backend credential.
`operator_connection` selects a deploy-named external-account grant (currently `google_mail` or
`google_calendar`),
`operator_login_identity` selects the acting Operator's console-login authority for hostexec token
exchange, and `none` injects nothing. The implementation registry declares the credential kind each
built-in accepts, and startup rejects a mismatch. The in-process servers are built like standalone
MCP servers from `@mcp.tool`-decorated functions:

- **`gmail`** (`haku.console.tools.gmail`). Reads mirror Gmail's REST API and return its
  resource shapes **verbatim** (`gmail_api.messages`/`gmail_api.labels`) — no content-type
  smartness, no body decoding, no flattening: `threads_list` (paginated via
  `page_token`/`next_page_token`), `threads_get`/`messages_get` (with a `format` argument that
  passes straight through to Gmail — `minimal`/`metadata`/`full`, plus `raw` for messages),
  `labels_list`, `labels_get`, `filters_list`, `filters_get`, `drafts_list`, `drafts_get`.
  Writes are draft CRUD (`drafts_create`, `drafts_update`, `drafts_delete`; never send),
  `threads_modify_labels`, label CRUD (`labels_create`, `labels_patch`, `labels_delete`), and
  filter create/delete (`filters_create`, `filters_delete`). Calls default to operator approval.
  The reviewed `v1` decision auto-approves Haku-agent calls to every
  read tool above, plus `threads_modify_labels` when every added/removed name starts with `haku/`,
  `labels_patch` only when both the current and new names start with `haku/` and no visibility is
  changed, and `labels_delete` only when the current label name starts with `haku/`. Patch/delete
  resolve the submitted label ID before deciding. Gmail affordances not
  yet exposed (send, trash/delete, message-level modify, attachments, history,
  non-filter settings) are tracked in `haku/console/TODO.md`.
- **`google_calendar`** (`haku.console.tools.google_calendar`). `create_event` creates a single
  event or an RRULE-backed series and stays operator-approved. `get_event`, `list_events`, and
  `list_event_instances` return focused recurrence-aware event models and auto-approve for
  authenticated Agents as transparent read tools. Deferred Calendar API affordances are inventoried
  in `TODO.md`.
- **`haku_routine`** (`haku.console.tools.routine`): `launch_routine` fires the Haku
  claude-code-web routine (optionally with per-run instruction `text`), so a launch is an
  ordinary approval-gated tool call rather than a bespoke capability. It uses the
  `haku-routine-launch-token` secret (`HAKU_CONSOLE_LAUNCH_ROUTINE__*`), not the Google grant, and
  supersedes the launch-routine capability tier above (kept during the haku-ui transition).

The `gmail` and `google_calendar` servers execute as the **acting Operator's own Google
account**: each call resolves that Operator's per-Operator Google access token from the
console's own connection store (`provider_connection.py`) through separate config-bound
`google_mail` and `google_calendar` connections, then builds the client for that one call — no
shared/startup credential. The console
holds each Google OAuth client and each Operator's refresh token itself (Postgres, self-refreshed
in-process), replacing Airlock's brokered `haku_console_google` token. Gmail requests
`gmail.modify`, `gmail.compose`, and `gmail.settings.basic`; Calendar requests `calendar.events`.
Until the corresponding connection is linked, that server is `degraded`. Both connections currently
use separate Google OAuth clients so their verification and credential lifecycles are independent.
Persisted
`connection_id` is the UUID of an actual association; it is distinct from the deploy name and
provider kind.

The trusted frontend resolves opaque ids by composing each server's ordinary MCP reads with its
Operator session. Gmail combines `threads_get` with `labels_list`; Calendar uses `list_events`;
Grocy and Tana likewise call their existing read tools. There are no preview-only MCP tools or
parallel preview-only HTTP routes.

## Free-form UI — Haku's own UI, embedded

The console frames Haku's own UI service (`haku-ui.allegedly.works`, a separate
Authentik-gated app Haku runs in `haku-sandbox`) as a **full-page sandboxed cross-origin
iframe** — it never renders or even sees the iframe's content. `HAKU_CONSOLE_HAKU_UI_URL`
enables it; the response CSP adds `frame-src` for that origin and Authentik's origin
(`HAKU_CONSOLE_AUTH_ORIGIN`, default `https://auth.allegedly.works`) so the in-frame SSO
redirect can complete.
Containment is cross-origin isolation: the iframe can't read the console's DOM/cookies or
act as it. The trusted **bridge** (`bridge.ts`) lets the iframe _request_ things via
postMessage — opening a link (`openLink`), launching a run (`requestLaunch`), and reading the
operator's location, either one-shot (`requestGeolocation`) or as a continuous stream the
shell holds (`startGeolocationWatch`); location is gated by a shell-owned standing consent
grant since the iframe has no `allow="geolocation"`. The shell origin-checks,
schema-validates, and decides/confirms before acting. It mirrors the iframe's validated route
(`routeChanged`) into the console's pathname so refresh/deep links — path-form URLs included —
restore the view (legacy `#/…` console URLs still restore). The shared bridge client also watches
the iframe's `<title>` and posts `titleChanged` automatically, so the outer tab follows it.
A persistent top-right floating toolbar (`shell_chrome.tsx`'s
`ShellChrome`, each button `filled` while its panel is selected) opens the shell's own trusted
chrome: a checklist button — badged with a callout light when a tool call is awaiting approval —
toggles the approval queue panel (with a link to the full-page past-tool-calls history); a gear
toggles the Settings panel (MCP account connect/reconnect/disconnect); a location-sharing pin
(shown only while consent is held, with a live indicator when location is actively read) toggles
a stop/withdraw panel; and a crossed-wifi button appears when the live event socket is down.
These controls behave as deselectable tabs, so at most one panel is open.
See <docs/containment.md>.

## Past tool calls — full-page history

Beyond the approvals panel's ephemeral "Recent" list, the console owns a **full-page history view**
of the authenticated operator's tool-call audit ledger (`frontend/tool_calls_page.tsx`), reached from the
approvals panel and living at its own route, `/tool-calls` (`frontend/routing.ts`). The console's own
`/tool-calls` path is reserved; every other path mirrors the framed haku-ui route, and the
shell renders the history view instead of the iframe when the reserved path matches. It reads `GET /api/tool-calls?newest_first=true`, so the
newest calls survive the query's limit. Production's nginx already serves the SPA for any
non-asset/API path; `app.py`'s dev fallback mirrors that so deep links work locally too.

## Layout

| Path                               | Role                                                                                                                                                                                                                                                                   |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`                           | Composition root for FastAPI, FastMCP, storage, execution, event delivery, and the shared `ToolCallApplicationService`; public app state exposes the service rather than its ledger/executor internals. Also serves config/health and the optional local SPA fallback. |
| `capabilities.py`                  | Capability-tier router (`/api/capabilities/*`): exact-Origin-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer and optional per-run text.                                                                        |
| `tool_call_service.py`             | Actor-scoped application boundary for policy evaluation, submit/read/wait, decisions, execution orchestration, and event publication.                                                                                                                                  |
| `mcp_approval.py`                  | Operator-browser FastAPI adapter plus the current Postgres tool-call repository, MCP executor, and metadata-reflection adapters. It does not own agent admission or lifecycle orchestration.                                                                           |
| `mcp_server.py`                    | FastMCP transport adapter for proxy and result-read tools; resolves its request dependency to a canonical `AgentActor` and delegates lifecycle operations to the application service.                                                                                  |
| `mcp_agent_auth.py`                | Agent-facing auth composition: one Haku OAuth adapter plus static-bearer verification, both resolving through the shared `PostgresAgentAuthority`.                                                                                                                     |
| `agents/`                          | Canonical Agent domain: naming, enrollment contracts/routes/APIs, and the transactional Postgres authority for interactions, grants, bindings, static credentials, activation, revocation, and expiry.                                                                 |
| `mcp_auth/`                        | Haku-owned FastMCP composition adapter and exact-version contract tests; contains the single accepted private `_code_store` seam.                                                                                                                                      |
| `console_events.py`                | Pydantic console-event shapes plus operator-scoped cross-replica WebSocket fan-out through Postgres LISTEN/NOTIFY.                                                                                                                                                     |
| `mcp_config.py`                    | Connected-MCP-server catalog plus in-process/remote transport and static bearer resolution, shared by the application service, reflection adapter, and operator OAuth linkage.                                                                                         |
| `in_process_servers.py`            | Canonical builder catalog for the Gmail, Google Calendar, and routine FastMCP servers, shared by the production app and schema exporter.                                                                                                                               |
| `mcp_operator_oauth.py`            | Operator OAuth account linkage for servers that execute as the operator's own account: the DCR/PKCE flow, association-specific client metadata, and the `/api/mcp/operator-auth/*` connect/disconnect/callback endpoints.                                              |
| `provider_connection.py`           | Deploy-named per-Operator connections to well-known external OAuth providers: fixed-client authorization-code + PKCE flow, connection-specific metadata, and the `/api/operator-connections/*` endpoints. Provider catalog: `provider_connection_registry.py`.         |
| `oauth_token_state.py`             | Shared current-token persistence and refresh state machine for remote-server, provider, and Operator-login OAuth associations. A short database claim deduplicates foreground and background refreshes across replicas.                                                |
| `oauth_association_maintenance.py` | Replica-coordinated candidate discovery and background refresh dispatch for expiring rows in the shared OAuth token-state table.                                                                                                                                       |
| `migrations/`                      | Alembic migrations for the deployed haku-console database; the console applies them at app startup before serving the API.                                                                                                                                             |
| `models.py`                        | Pydantic `ConfigResponse` — the `/api/config` response model.                                                                                                                                                                                                          |
| `config.py`                        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                                                                       |
| `export_schema.py`                 | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                                                        |
| `export_mcp_tool_schemas.py`       | Reflects the real in-process servers through MCP `tools/list` and prints their exact input schemas for generated frontend validators and types.                                                                                                                        |
| `frontend/`                        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                                                                 |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
The deployment runs two containers in one pod: the `haku-console` FastAPI API
image and a separate `haku-console-static` nginx image that bakes in the
fingerprinted SPA. nginx serves `/` and `/assets/*`, proxies `/api/*`, `/healthz`,
`/mcp`, `/auth/*`, and the `/.well-known/oauth-*` discovery paths to FastAPI on
localhost, and sets cache policy by route (`/assets/*` immutable, app shell
never stored, missing assets and API/health/mcp/auth uncached). No runtime asset copy or shared web
volume is used.

The Deployment uses `Recreate` because each static image contains only its current bundle. This
creates an atomic version boundary: a page cannot receive HTML from one replica version and request
its fingerprinted asset from another. It also means the console is wholly unavailable until a
replacement is Ready; a broken release causes an unbounded outage and must be fixed forward.

The server receives both Flux-selected image tags through `HAKU_CONSOLE_{,STATIC_}IMAGE_TAG`.
`GET /api/deployment` parses their commit suffixes at runtime for the Settings version links. This
keeps build stamping out of the OCI layers, so image contents remain reproducible.

**Auth is app-owned** (the Authentik forward-auth proxy outpost that used to gate
`haku.allegedly.works` is retired; the HTTPRoute now points straight at the console
Service). The operator browser logs in via Authentik OIDC (`HAKU_CONSOLE_OPERATOR_OIDC__*`
→ signed session cookie; router-level dependency guards protect `/api/*`), and agents authenticate to the always-mounted
`/mcp` through `HakuAgentOAuthProxy` plus static-bearer verification; both resolve canonical Agents
through one failure-preserving authority (`HAKU_CONSOLE_MCP_OAUTH__*`; FastMCP registration/token
state persists in the console's own Postgres via `HAKU_CONSOLE_MCP_OAUTH__PERSISTENCE__*`).
`HAKU_CONSOLE_PUBLIC_BASE_URL` is the single canonical public origin for both flows: operator login
uses `<origin>/auth/callback`, while the agent-facing OAuth issuer and callback are derived as
`<origin>/mcp` and `<origin>/mcp/auth/callback`. Only OAuth discovery is also exposed at the origin's
standard `/.well-known/oauth-*` paths; the two clients' credentials, sessions, and operational
routes remain separate.
The console refuses to start without operator OIDC or with no `/mcp` credential at all (no static
agent and no `mcp_oauth`).
Both OAuth2 providers and their client secrets
(the `haku-console-oidc` Secret) are minted by `tf/gitops/agent-machine-access`; single-user
access is Authentik's application access policy.

Postgres is **required**: it backs Operator/Agent authority, the approval ledger, FastMCP state, and
the Operator OAuth token store. The console applies its Alembic baseline once at startup (`app.main`,
before serving) — never as a side effect of constructing a store. Baseline `0010` directly creates
the canonical Operator/identity and Agent/name/binding/grant/tool-principal graph. Its revision ID is
deliberately retained from the deployed migration lineage: a database already stamped `0010` is a
no-op, while a fresh database creates the same frozen schema.

Non-root, dropped caps, no service-account token. Credentials: the
`haku-routine-launch-token` secret (the launch capability bearer; `HAKU_CONSOLE_LAUNCH_ROUTINE__TOKEN`),
the OAuth client secrets (`haku-console-oidc`), the config-file/database settings
(`HAKU_CONSOLE_CONFIG_FILE`, `HAKU_CONSOLE_DATABASE_URL`, `HAKU_CONSOLE_PUBLIC_BASE_URL` — the OAuth
redirect-URI origin), and each `static_agents` entry's env-referenced bearer + startup identity seed
(e.g. `HAKU_CONSOLE_AGENT_HAKU_TOKEN` from `haku-console-agent-api`, and the externally stable
`HAKU_CONSOLE_AGENT_HAKU_OPERATOR` / `operator_subject` label from the TF-fed `haku-console-oidc`
Secret). That Authentik `sub_mode=user_id` value is used only to create/find an identity anchor and
is immediately resolved to an Operator UUID; it is never carried as live request authority.
It no longer holds a haku-state git credential — feedback/trace writes moved into haku-ui.
As trusted ducktape code in its own namespace it is **not** behind the `haku-egress-proxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). Security model: `haku/docs/security.md`; roadmap: `haku/PLAN.md` and the
`haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
