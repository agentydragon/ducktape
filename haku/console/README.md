# haku/console — Haku's interactive console

A small FastAPI service that serves the Haku console as a **React single-page app
over a JSON API**. It is the trusted operator interface: Authentik operator-only,
reviewed ducktape code. It replaced the static nginx + git-sync dashboard (now retired).

All product surfaces have **moved** to haku-state's `ui/` — Haku's own UI service
(`haku-ui`), which the console frames **full-page** as a sandboxed cross-origin iframe. The
console is now just the trusted outer shell: the capability tier (launch-routine) plus the
bridge that brokers the iframe's privileged requests. It holds Haku's Forgejo credential, but
only ever reads with it (the `haku_index` git corpus, below).

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
straight into haku-state from haku-ui (which Haku already owns), so nothing here writes to
haku-state. The console does keep a **read-only bare mirror** of it, fetched by the index's
sync sweep — see `haku_index` below.

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
console-side lookup/schema errors still fail closed to manual review.

A queued call has exactly three exits, and each belongs to one actor: the operator **approves** it
(→ `running`) or **denies** it (→ `denied`), or the submitting Agent **withdraws** it
(→ `withdrawn`) via the MCP `withdraw_tool_call` tool. Withdrawal is the requester retracting an
ask it no longer wants — a superseded plan, a duplicate, something the operator already did by
hand — so nobody is asked to decide on abandoned work. It is deliberately a separate status from
`denied`, which records a human's judgment, and it is agent-only: an operator dismissing a call
uses `deny`. Withdrawal is scoped to the owning **Agent**, not the exact credential binding, so an
Agent that reconnected can still clear its predecessor binding's ask out of the queue. Only a
`pending_approval` call can be withdrawn; approval and withdrawal race under the tool-call row lock,
and the loser is told the winner's status. haku-state stores only authored requests
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
  wakes the shell to refresh. It carries chat sessions too: a `session_changed` names the session
  whose rows moved and nothing else, coalesced to at most one per session per half-second, so a
  streaming turn's per-delta writes cost the conversation inventory two re-reads a second rather
  than hundreds (`x/session_live_updates.py`). An open transcript is not one of its consumers — it
  follows its own conversation's socket.
- `POST /api/tool-calls/{tool_call_id}/decision` — exact-Origin-gated trusted-frontend approval/denial.
- `GET /api/tool-calls` / `GET /api/tool-calls/{tool_call_id}` — operator-only audit/result reads.
  The list endpoint accepts repeated `status` filters, a datetime `since` filter on `updated_at`,
  and an `auto_approved` filter on whether the call carries an `approval_policy_id`. It pages by
  keyset, not offset: each response carries a `next_cursor` (null once the page is the last) to
  pass back as `cursor`. Offset paging would skip or repeat rows, since calls are submitted into
  the top of the order between a page and the next.
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
Direct Operator calls do not create tool-call rows, approval events, or non-terminal stubs; they exist so
trusted renderers can resolve reference data through the real MCP surface instead of bespoke HTTP
fetchers.

Discovery is request-local: either principal sees remote servers connected by its canonical
Operator, plus shared configured/in-process servers. For Agents, that surface is divided into two
buckets. Tools the caller's access profile **unconditionally** auto-approves — through the policy
graph in <../../cluster/k8s/haku/console/config.yaml> — appear as
transparent **pass-throughs** (original schema, real result); everything else keeps the same
`<server>__<tool>` name but uses an envelope `{input, title?, rationale, wait_for_result_ms?}` that
returns the real result if approval and execution reach a terminal state within the wait, else a
**non-terminal stub** (a `tool_call_id` + an operator-facing deep-link `url`) the agent resolves via
the `get_tool_call` / `list_tool_calls` read tools. A `pending_approval` stub means the operator did
not approve or deny before the requested wait ended; it is not an expiry or cancellation. The call
remains in the queue, may be approved or denied later, and executes if later approved. A `running`
stub means approval happened within the wait but downstream execution is still in progress. The
agent can retract a still-pending stub via `withdraw_tool_call(tool_call_id, reason)` — the one console-native
ledger mutation, annotated `readOnlyHint=False`.

`call_mcp_tool(server_id, tool_name, arguments?)` is the generic fallback for reaching a tool that
has no generated proxy in the caller's tool list. **Gotcha it exists for:** discovery is
request-local but a connected client enumerates once, so a server that was `degraded` at connect
time contributes no tools for the life of that session — it keeps serving, and the agent cannot name
anything on it. This closes that gap without a reconnect.

`arguments` is **exactly what the generated tool would have taken** — raw upstream arguments for a
pass-through tool, the `{input, rationale, …}` envelope for one that needs approval — so the payload
is the same call, only addressed by name. There is one parse and one dispatch: `_dispatch` reads the
payload and runs it, and `ProxyTool.run` is pure delegation to it. So the policy decision, the schema
check that makes a bad call born-denied, the audit row's real `(server_id, tool_name)`, and the
Operator direct-execution path are the same code for both routes — naming a tool here cannot escape
approval. Annotated `openWorldHint=True` with `destructiveHint` left unset, because the target is a
parameter rather than a property of the tool.

**Reflection reports the exposed view, not the upstream one.** `get_mcp_server_status` describes each
tool as _this proxy_ presents it to the asking caller: `input_schema` is the schema the console
accepts — already enveloped where the policy requires approval — and `approval_mode` names which of
the two shapes that is. The upstream schema is not returned alongside it; for an enveloped tool it is
already nested under `input`, so reporting both would be the same schema twice. Both fields are
per-caller, since the policy is per-Agent, and both come from the one `AutoApprovalPolicyRegistry`
the generated proxies use, so what reflection says about a tool's payload shape cannot disagree with
what dispatch accepts. That is what a caller of `call_mcp_tool` reads to learn which shape to send.

The reflected `instructions` are the upstream server's own `initialize` guidance, passed through
rather than restated. The handshake happens on every reflection anyway, so this was already being
fetched and discarded.

**The two reflection tools split on whether they touch the network.** `list_mcp_servers` is passive:
the configured catalog plus each persisted per-Operator OAuth/provider status, never a credential
refresh and never a downstream connect. Background reconciliation, rather than `tools/list`, owns
live discovery.
Each server's discriminated `backend` mirrors the safe configuration shape, and no status object
ever carries an access token, refresh token, client secret, or static-bearer secret reference. A
cataloged provider connection whose deploy-time OAuth client is absent reports
`status: unprovisioned` rather than disappearing, and Settings renders that with no Connect action;
an auth kind with no separate operator-linked connection reports `connection: null`.
`get_mcp_server_status(server_id)` is the active counterpart — it may refresh the operator's
credentials and contact the server — and omits the potentially large input schemas unless the caller
passes `include_tool_schemas=true`. Credential-resolution and discovery failures come back as
distinct degraded stages and reasons instead of failing the reflection call, and a direct call in a
known `<server>__<tool>` namespace returns that same actionable error rather than "unknown tool".
The stub-semantics preamble lives in each tool's **description**, because many MCP clients —
claude.ai included — never surface a server's `instructions`.

Auth is a Haku-owned `HakuAgentOAuthProxy` composed with the configured `static_agents` through
`HakuFailurePreservingMultiAuth`. An explicit `Authorization` header always selects Agent admission;
an invalid bearer never falls back to an ambient browser cookie. FastMCP still owns DCR, PKCE,
callback, code, and token-family machinery; Haku adds the Operator-authenticated Agent-enrollment
ceremony and resolves both OAuth and static credentials through the same canonical Agent authority,
with OAuth state required to live in the console's Postgres rather than FastMCP's process-local
store or Valkey. The `/mcp` surface submits, reads, and lets an Agent **withdraw its own
still-pending call** — there is no decision tool, so an OAuth caller cannot self-approve, and
withdrawal only ever moves a call the caller itself queued toward a terminal state, never toward
execution. Approval stays in trusted console chrome, and approved calls execute against the
console's stored Operator credentials, so an incoming token's blast radius is "call the console's
submit/read/withdraw-own tools" and nothing else.

The trusted frontend validates the console-native reflection results with the same generated MCP
result-schema catalog used by Gmail, Google Calendar, Grocy, and routine renderers. The catalog is
generated from the Python response models at build time; the frontend does not restate those wire
models in handwritten Zod.

#### Catalog reconciliation

`tools/list` is a snapshot read: it performs no downstream MCP connect and no OAuth token refresh.
Each replica reconciles every active Operator's configured servers before its MCP endpoint becomes
ready, then refreshes them throughout the process lifetime at
`HAKU_CONSOLE_MCP_CATALOG_REFRESH_INTERVAL_SECONDS` (default 60) — see
`mcp_catalog_reconciler.py`. One complete per-Operator generation is published atomically, so a
request sees the previous generation while the next concurrent fan-out is still running. A newly
admitted Operator queues an immediate background pass; the first listing remains non-blocking and
may be empty until that pass publishes.

OAuth and provider connection-change events cross replicas through the existing Postgres
`LISTEN`/`NOTIFY` stream. Each replica immediately removes that Operator's generation and queues a
replacement, so a disconnect cannot leave callable-looking proxy tools around for the periodic
refresh window.

The dispatcher still reuses each successful reflection for the same interval and collapses
concurrent work by `(server_id, config fingerprint, credential fingerprint)`. The key contains a
digest rather than a bearer, and a rotated credential cannot reuse the previous holder's reflection.
Failed credential resolution or discovery publishes a degraded snapshot, which contributes no proxy
tools. Tool execution never trusts the snapshot as authority: it revalidates the Operator/Agent
binding and resolves the current server credential independently.

Nothing invalidates immediately when an upstream changes its tools, so the refresh interval is the
routine staleness budget. Persistent upstream sessions plus
`notifications/tools/list_changed` remain a possible lower-latency refinement, but they are no
longer required to keep network and OAuth latency out of the client discovery path.

### Canonical Agent authority and enrollment

Alembic revision `0081` is the single forward-only database baseline: one Postgres graph shared by
interactive OAuth and configured static Agents, where `Operator`, `Agent` and every
authority-bearing relationship are local immutable UUIDs, a `CredentialBinding` owns credential
lifecycle, and an Agent-originated tool call persists only its exact binding provenance — so
approval and execution revalidate that binding and queued work cannot transfer to a replacement
credential. Enrollment is an Operator-authenticated browser ceremony layered over FastMCP's own
authorize/callback/token path; the runtime caller it produces is `OperatorActor | AgentActor`, and
an Agent's config-defined access profile is chosen at enrollment. The profile bundles its
auto-approval policy and named logical Recall-index grants. A missing or removed profile is
fail-closed; the configured enrollment default is a reviewed deployment choice. Agents submit and
read only their own calls, and never approve themselves.

The durable contract, the eight-step ceremony, the FastMCP version-pinning seam, and the accepted
credential boundary: <docs/agent_authority.md>.

### In-process MCP servers — no second deployment

An `mcp.servers` entry explicitly selects either a `remote_mcp` backend with an HTTP URL and
transport `auth`, or an `in_process` backend with an implementation `credential`. The latter is a
registered **in-process `FastMCP` instance**: `fastmcp.client.Client` accepts a `FastMCP` object
directly (an in-memory `FastMCPTransport`), so `McpServerDispatcher` runs the exact
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
- **`haku_index`** (`haku.console.tools.recall_index`): `search` is semantic recall over one
  explicit configured logical index: Git source at the indexed tips of `haku-state` and public
  `ducktape-public`, or the console's own record of past chat sessions. The authenticated durable
  Agent's configured access profile is the authority boundary: its `recall_index_ids` grant is
  checked before embedding or querying, and no MCP argument can expand that grant. Missing or unknown
  profiles fail closed. `index_status` reports only the same permitted logical indexes. The separate
  `haku_conversations` server is instead governed by its profile's explicit
  `in_process_server_ids` grant: server access is not an index grant and is not an auto-approval
  decision. Direct `OperatorActor` calls can read every configured Recall index; they do not inherit
  an Agent profile. Search returns each
  matching indexed chunk by default, plus a Git index id, path, commit, and blob sha, or a session,
  room, and message ids to read through `haku_conversations`. Set `include_content=false` for
  provenance without chunk text; returned chunks remain retrieval context rather than an authoritative
  whole-source read. A search whose selected index is behind attaches that index's status to its own
  result; `index_status` distinguishes "not indexed yet", "indexing now", and "cannot reach the
  repository". Listing the server in `config.yaml` is what builds it, and the console refuses to
  start if it is listed without an embedder configured — search embeds its query, so a search tool
  with nowhere to embed is a tool
  that can only fail. The chat corpus is the console's own database; `haku-state` uses its configured
  Forgejo credential, while the public Ducktape clone is anonymous. **Source materialization and
  embedding run as separate maintenance stages** (`recall_index_sync.py`): a chat sweep every minute
  over the console's own tables and one Git poll every thirty seconds per bare mirror on the pod's
  `/tmp` materialize source chunks without calling the embedding provider. A separate shared worker
  drains the globally de-duplicated pending content queue for the active model. Source leadership is
  per index and embedding leadership is per model, so a long Git fetch and a cold embedding batch do
  not block one another; status reports pending chunks until the source material becomes searchable.
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
postMessage — opening a link (`openLink`), launching a run (`requestLaunch`), reading the
operator's location either one-shot (`requestGeolocation`) or as a continuous stream the
shell holds (`startGeolocationWatch`), and grabbing a frame of the shell's own on-screen rect
(`requestScreenshot`). The last two are gated by shell-owned standing consent grants, since the
iframe has neither `allow="geolocation"` nor `allow="display-capture"`. The shell origin-checks,
schema-validates, and decides/confirms before acting. It mirrors the iframe's validated route
(`routeChanged`) into the console's pathname so refresh/deep links — path-form URLs included —
restore the view (legacy `#/…` console URLs still restore). The shared bridge client also watches
the iframe's `<title>` and posts `titleChanged` automatically, so the outer tab follows it.
A fixed-width icon rail down the left edge (`shell_chrome.tsx`'s `ShellChrome`) is the shell's own
trusted chrome, and it reserves layout space the frame cannot render into. Its top button — badged
with a count while tool calls await approval — toggles the approval queue drawer, independently of
whichever page is selected; below it, page buttons select the framed Haku UI, Conversations,
Settings, or the past-tool-calls history. The bottom holds indicators, which
are mutually exclusive popovers: sync state (always present; an expired session closes the event
socket with its own code, `4001`, so the shell re-authenticates instead of reporting a channel
outage), a location pin and a camera (each shown only while its consent grant is held, with a live
dot while it is being read) opening stop/withdraw panels, and a clock in the last few minutes of
the operator session opening a re-authenticate panel. See <docs/containment.md>.

## Past tool calls — full-page history

Beyond the approvals panel's ephemeral "Recent" list, the console owns a **full-page history view**
of the authenticated operator's tool-call audit ledger (`frontend/tool_calls_page.tsx`), reached from the
rail and from the approvals drawer, living at its own route, `/_console/tool-calls`
(`frontend/routing.ts`). `/_console` is the
one pathname namespace the console reserves for itself; every other path mirrors the framed haku-ui
route, and the shell renders a console page instead of the iframe when a reserved path matches. It
reads `GET /api/tool-calls?newest_first=true`, so the
newest calls come first, and walks older ones a page at a time by following `next_cursor` ("Load
older calls"). Production's nginx already serves the SPA for any
non-asset/API path; `app.py`'s dev fallback mirrors that so deep links work locally too.

**The page is small on purpose** (25 rows). A record carries its whole arguments and result
payload, so asking for the endpoint's `le=500` cap meant a multi-megabyte response — and one
syntax-highlighted code block built per row, which blocked the browser's main thread for seconds
before anything was usable. Two properties keep it that way, and a change here should preserve
both: a live event refetches only the **first** page and merges it over what is loaded
(`mergeNewestPage`) rather than refetching everything, with at most one refresh in flight so a
burst of events collapses into one catch-up; and a code block builds its editor only once it
nears the viewport (`frontend/code_block.tsx`), so rows below the fold cost a placeholder.

A "Show auto-approved" checkbox (unchecked by default) toggles the `auto_approved` query param on
that request, so the routine, unconditionally auto-approved traffic (Gmail/Calendar reads, etc.)
doesn't bury the calls an operator actually had to decide on. The filter runs server-side (`WHERE
approval_policy_id IS NULL`, in `PostgresToolCallLedger.list_tool_calls`) rather than over-fetching
and discarding client-side, so it doesn't starve the page of older manual calls once auto-approved
traffic fills a page. The same `auto_approved` parameter is threaded through
`ToolCallApplicationService.list_tool_calls` to the agent-facing MCP `list_tool_calls` tool
(`mcp_server.py`), so an agent reviewing its own call history can filter the same way.

## Notifications — Web Push for pending approvals

The console's event socket keeps open tabs current, but it reaches nobody when no browser has the
console loaded — which is exactly when a queued call most needs to find the operator. **Web Push**
covers that gap: the operator turns notifications on per browser in Settings → Notifications, and
the console pushes an OS notification carrying **Approve** / **Deny** whenever a call enters the
queue (plus **Details** where the platform has room — browsers cap notification actions and
silently drop the rest, and Chrome's cap is two; tapping the notification body opens the call
regardless, so nothing is lost where Details cannot be shown). A destructive call is marked in
the title, mirroring the red the approvals card gives its action line — a notification has no
color, and it is the one surface where such a call can be approved without its arguments being
seen. Server half: `web_push.py` (delivery) plus `push_routes.py` (`/api/push/*`, subscriptions).
Browser half: `frontend/sw.ts` (the service worker) plus `frontend/push_subscription.ts`.

**No new authority.** A push is a prompt to decide, never the decision. The notification's buttons
are defined in the console's own service worker and act through the ordinary
`POST /api/tool-calls/{id}/decision`, as a same-origin credentialed fetch under the operator's
Authentik session — the same endpoint and the same guard as a click in the approvals drawer.
Intercepting a push therefore grants nothing. This is also why notifications go through Web Push
from the console's own origin rather than a notification service (ntfy, Telegram) with action
buttons: those would have to carry a deciding credential inside a message on a third-party server,
against <../docs/security.md> invariant #4.

**Notifications are retracted, not left to rot.** `PendingApprovalNotifier` (a port on
`ToolCallApplicationService`) fires when a call enters the queue and again on each of the three
exits — denied, approved, withdrawn — so a notification on the phone stops offering buttons for a
call that was already decided at the desk. Retraction happens at the _decision_, not at execution:
the ask is settled the moment it is approved. A retraction replaces the notification in place with
its outcome rather than silently closing it, because Chrome requires a `userVisibleOnly`
subscription to show something per push and substitutes its own "site updated in the background"
notice once an origin's push budget is spent showing nothing. Calls that never queue —
auto-approved or born-denied — are never notified and so never retracted.

Two operational notes:

- **The VAPID keypair is the console's identity to every push service.** Only the private half is
  configured (`HAKU_CONSOLE_WEB_PUSH__PRIVATE_KEY_PEM`, from `haku-console-web-push-vapid`); the
  public half is derived at startup, so the two cannot drift apart. Rotating it invalidates every
  stored subscription — each device must re-subscribe from Settings.
- **The push payload is a versioned wire contract.** The console deploys atomically, but the
  service worker that reads its messages updates only when the browser checks — on a navigation to
  the console, or after a push once the registration is stale (>24h). An installed worker can
  therefore be a day behind the server pushing to it, so `PushShow`/`PushRetract` fields may be
  added but never renamed or removed; a non-additive change needs a new `kind` variant. Both sides
  say so (`web_push.py`, `frontend/sw.ts`).
- **Operator sessions are short** (`OPERATOR_SESSION_MAX_AGE_SECONDS`, one hour), so a notification
  acted on hours later routinely outlives the session that would authorize it. The service worker
  treats that 401 as expected and opens the console at the call's deep link
  (`/_console/tool-calls/<id>`) to re-authenticate, rather than reporting a failure. Deciding from
  the lock screen is therefore one tap while the session is fresh and two taps otherwise; shortening
  that would mean revisiting the session lifetime, not the push plumbing.

## Layout

| Path                               | Role                                                                                                                                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`                           | Composition root for FastAPI, FastMCP, storage, execution, event delivery, and the shared `ToolCallApplicationService`; public app state exposes the service rather than its ledger/executor internals. Also serves config/health and the optional local SPA fallback.       |
| `capabilities.py`                  | Capability-tier router (`/api/capabilities/*`): exact-Origin-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer and optional per-run text.                                                                              |
| `tool_call_service.py`             | Actor-scoped application boundary for policy evaluation, submit/read/wait, decisions, execution orchestration, and event publication.                                                                                                                                        |
| `mcp_approval.py`                  | Operator-browser FastAPI adapter plus the current Postgres tool-call repository and `McpServerDispatcher` — the one client for the configured MCP servers, which both executes tool calls and reflects catalogs. It does not own agent admission or lifecycle orchestration. |
| `mcp_server.py`                    | FastMCP transport adapter for proxy and result-read tools; resolves its request dependency to a canonical `AgentActor` and delegates lifecycle operations to the application service.                                                                                        |
| `mcp_agent_auth.py`                | Agent-facing auth composition: one Haku OAuth adapter plus static-bearer verification, both resolving through the shared `PostgresAgentAuthority`.                                                                                                                           |
| `agents/`                          | Canonical Agent domain: naming, enrollment contracts/routes/APIs, and the transactional Postgres authority for interactions, grants, bindings, static credentials, activation, revocation, and expiry.                                                                       |
| `mcp_auth/`                        | Haku-owned FastMCP composition adapter and exact-version contract tests; contains the single accepted private `_code_store` seam.                                                                                                                                            |
| `console_events.py`                | Pydantic console-event shapes plus operator-scoped cross-replica WebSocket fan-out through Postgres LISTEN/NOTIFY. `deliver_locally` is the entry point for a producer already broadcast by another channel.                                                                 |
| `web_push.py`                      | Web Push delivery of pending-approval notifications: the VAPID identity, the per-Operator subscription store, and the notifier that shows and retracts one call's notification.                                                                                              |
| `push_routes.py`                   | Operator-browser `/api/push/*` surface: the public VAPID key the SPA subscribes with, plus subscription registration, listing, and removal.                                                                                                                                  |
| `mcp_config.py`                    | Connected-MCP-server catalog plus in-process/remote transport and static bearer resolution, shared by the application service, `McpServerDispatcher`, and operator OAuth linkage.                                                                                            |
| `mcp_reflection_cache.py`          | Short-lived reuse inside catalog reconciliation: a TTL plus single-flight, keyed so a reflected catalog never crosses credential revisions.                                                                                                                                  |
| `mcp_catalog_reconciler.py`        | Startup and continuous per-Operator catalog reconciliation; atomically publishes the in-memory generations read by `tools/list`.                                                                                                                                             |
| `in_process_servers.py`            | Canonical builder catalog for the Gmail, Google Calendar, routine, conversations, index, and hostexec FastMCP servers, shared by the production app and schema exporter.                                                                                                     |
| `recall_index_reader.py`           | Binds the `haku_index` tools to the console's database and embedder; it resolves configured logical index ids to their `git`/`chat` source shapes.                                                                                                                           |
| `recall_index_sync.py`             | The sweeps that keep every configured index current: chat from the console's own tables and Git sources from their bare mirrors. One Postgres advisory lock per index, so one replica syncs each.                                                                            |
| `mcp_operator_oauth.py`            | Operator OAuth account linkage for servers that execute as the operator's own account: the DCR/PKCE flow, association-specific client metadata, and the `/api/mcp/operator-auth/*` connect/disconnect/callback endpoints.                                                    |
| `provider_connection.py`           | Deploy-named per-Operator connections to well-known external OAuth providers: fixed-client authorization-code + PKCE flow, connection-specific metadata, and the `/api/operator-connections/*` endpoints. Provider catalog: `provider_connection_registry.py`.               |
| `oauth_token_state.py`             | Shared current-token persistence and refresh state machine for remote-server, provider, and Operator-login OAuth associations. A short database claim deduplicates foreground and background refreshes across replicas.                                                      |
| `oauth_association_maintenance.py` | Replica-coordinated candidate discovery and background refresh dispatch for expiring rows in the shared OAuth token-state table.                                                                                                                                             |
| `migrations/`                      | Alembic migrations for the deployed haku-console database; the console applies them at app startup before serving the API.                                                                                                                                                   |
| `models.py`                        | Pydantic `ConfigResponse` — the `/api/config` response model.                                                                                                                                                                                                                |
| `config.py`                        | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                                                                             |
| `export_schema.py`                 | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                                                              |
| `export_mcp_tool_schemas.py`       | Reflects the real in-process servers through MCP `tools/list` and prints their exact input schemas for generated frontend validators and types.                                                                                                                              |
| `frontend/`                        | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                                                                       |
| `x/`                               | Experimental surfaces the console runs but does not promise: the session runtime at the top level, one directory per channel (`x/channels/matrix/`) and per CLI harness (`x/claude_code/`) — see `x/README.md`.                                                              |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
The API and static shell run as separate Deployments. `haku-console` is the FastAPI API; the
unprivileged `haku-console-static` nginx Deployment bakes in the fingerprinted SPA, serves `/` and
`/_console/assets/`, and proxies `/api/*`, `/healthz`, `/mcp`, `/auth/*`, and the
`/.well-known/oauth-*` discovery paths to the API Service. A static-only image change therefore
does not restart API auth, MCP streams, or background workers. nginx sets cache policy by route
(fingerprinted assets immutable, app shell never stored, missing assets and API/health/mcp/auth
uncached). **Invariant:** a new top-level backend prefix needs its own `location`, or it falls
through to the SPA shell and returns `index.html` instead of reaching the app (this is what bit
`/mcp`). No runtime asset copy or shared web volume is used. It also gzips both what it serves and
what it proxies (`gzip_proxied any` — nginx leaves upstream responses alone otherwise), covering
the ~1.8 MB SPA bundle and the API's JSON; `text/event-stream` is deliberately left out so the
`/mcp` stream still flushes per event.

The API reports the **Flux-selected** static version from a projected
`haku-console-static-metadata` ConfigMap rather than an environment variable in its own pod
template. Flux updates that ConfigMap alongside the static image, so Settings can report both
selected revisions without making the API roll for frontend-only metadata. It is desired-state
metadata, not proof that every static replica has completed its rollout.

The API pods are pinned to the **`hil-ovh` zone**, where both their Postgres and the public ingress
live. This is a latency constraint, not a preference: an operator API call opens a database session
per read and each session costs several round trips, so a replica a WAN hop away turned a 4.6ms
query into a two-second request. It costs no availability — the API cannot serve without that
node-pinned database anyway. The static Deployment has no database dependency and is not pinned.

The Deployment rolls with **`maxUnavailable: 0`**, so a replacement that never becomes Ready leaves
the running version serving and a bad release is a no-op instead of an outage. It replaced
`Recreate`, which deleted every pod before starting one: on 2026-08-10 a transiently missing
reflected Secret (`ha-mcp-bearer`, gone for ~2 minutes while its own namespace churned) took the
console fully down, where rolling would have been invisible.

What follows from it, all of it real:

- **Assets can skew during a static roll.** Each static image contains only its own fingerprinted
  bundle, so a browser can take the new shell from one static replica and 404 on its chunk against
  the other. The window is the static roll's length and a refresh afterwards fixes it. Closing it
  needs session persistence so a page load stays on one replica — Service `sessionAffinity` is not
  obviously honored through Cilium's Gateway API path (Envoy load-balances endpoints itself), so
  that wants verifying before it is added rather than being configured hopefully. API/static
  compatibility must remain additive across independent rolls.
- **Migrations must be backward compatible for the length of a roll.** `Recreate` guaranteed no old
  pod outlived the migration; rolling does not, so old code runs against the new schema for a
  minute. Additive changes are fine. A destructive one (dropping or renaming a column an old
  replica still selects) has to be split expand/contract across two releases — the constraint the
  previous strategy hid. Two is the floor, not the count: an ORM-mapped column is named in every
  `SELECT` SQLAlchemy emits whether or not any code reads the attribute, so dropping one takes three
  — add the replacement, unmap the old column, then drop it a release after the unmapping converged.
- **Vocabularies must be readable by the release before them.** The rule above is about columns; the
  same roll puts _values_ in front of an older reader — a new enum member, a new event kind, a new
  field on a payload that crosses replicas. The answer has a different shape, so it is its own
  section: § Vocabularies across a roll, below.

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

The operator session carries an **absolute one-hour deadline** signed into the cookie payload; it
never slides, and `GET /auth/me` reports it so the shell can warn before it rather than letting a
background request 401 and navigate the tab away. Each pending login is a row in
`operator_login_flows` (`operator_login_flow.py`) rather than session-cookie state — deviation from
stock authlib, whose Starlette integration keeps one pending authorization per browser and so
strands every console tab but the last whenever they re-authenticate together. Its user-agent
binding is preserved per flow: a secret in a cookie **named after that flow's `state`**, so
concurrent attempts cannot overwrite each other. A stale or superseded callback restarts the login
once by itself (bounded by a marker cookie) instead of dead-ending, and both `/auth/login` and the
frontend's 401 redirect carry a `return_to` — any local console page — so re-authenticating comes
back to the view the operator was on.

Both OAuth2 providers and their client secrets
(the `haku-console-oidc` Secret) are minted by `tf/gitops/agent-machine-access`; single-user
access is Authentik's application access policy.

Postgres is **required**: it backs Operator/Agent authority, the approval ledger, FastMCP state, and
the Operator OAuth token store. The console applies its Alembic baseline once at startup (`app.main`,
before serving) — never as a side effect of constructing a store. Since the Deployment rolls rather
than recreating, a migration runs while the previous version is still serving, so each one must be
backward compatible for the length of a roll (see Perimeter / deploy). Baseline `0081` directly creates
the canonical Operator/identity and Agent/name/binding/grant/tool-principal graph. Its revision ID is
deliberately retained from the deployed migration lineage: a database already stamped `0081` is a
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
It also holds Haku's own Forgejo credential (`haku-forgejo-git`, reflected in from haku-sandbox;
`HAKU_CONSOLE_HAKU_STATE_GIT__*`), which the index's git sweep fetches haku-state with. The shared
retrieval-unit budget, including `HAKU_CONSOLE_RECALL_INDEX__CHUNK_BUDGET__OVERLAP_CODEPOINTS`, configures both
index sweeps and search readers so they select the same chunk regime. Nothing in
the console writes to haku-state — feedback/trace writes moved into haku-ui — but this credential
_can_, which is the cost of reusing Haku's account instead of provisioning a second, read-only one
(operator, 2026-08-15). The console is more trusted code than Haku, which already holds it.
As trusted ducktape code in its own namespace it is **not** behind the `haku-egress-proxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). Security model: `haku/docs/security.md`; roadmap: `haku/PLAN.md` and the
`haku-state` repo's `plans/dashboard-arm.md`.

### Vocabularies across a roll

A new writer meeting an old reader fails **transiently**: it dies with the replica. An old writer
meeting a new reader fails **permanently**: it dies with the row. Tolerance fixes the first. Only a
constraint fixes the second.

**Readers tolerate.** Decoding a stored value, or a payload from a sibling replica, must not raise
on something the reader has no words for. It decodes to a named "I do not know this" —
`util.sqlalchemy_types.UnknownValue` for a column, `x/session_events.UnknownEventBody` for a row,
never `None` and never a nearby member — so every consumer is made to say what it does with one.
Models that cross replicas do not forbid unknown fields; `x/session_notifications`,
<console_events.py> and `x/session_events` all say so where they define their shapes.

**Writers still wait, unless ignoring is correct.** Tolerance only ever buys "ignore it correctly",
so whether the writer may ship in the same release depends on what kind of vocabulary it is:

- **Narration** — an append-only stream whose readers render or fold it: `session_events.kind`, the
  notification channel's kinds, `ConsoleEvent.event_type`. An older reader passing over an entry it
  has no words for is the correct behaviour, not a degradation, so **a new value ships with its
  writer in one release**. What it costs is named where it is paid: the room never says the notice
  it skipped, because the cursor moves past it (`channels/matrix/room_subscription.notice`).
- **Decision** — a value a reader branches on: `SessionStatus`, `TurnOutcome`, `ToolCallStatus`,
  `EventProvenance`, `PromptRejection`. No reader-side answer is correct — an unknown `SessionStatus`
  is neither open nor ended, and guessing either lets the lease sweep fail a live session or lets a
  dead one leak — so **the reader ships a release ahead of the writer**, gated on that roll having
  converged. `0054` did exactly this and `0077` cleaned up after it.

The two are separable, and the second could take tolerance without taking the single release:
decoding to `UnknownValue` would keep one unreadable row from failing a `select(Session)` over the
whole inventory, while the two-release rule still stood because no consumer may guess what the value
means. Nothing does that today — the decision columns stay strict.

**A required field added to an existing shape is neither.** It is a narrowing, and tolerance is the
wrong tool: a tolerant new reader would silently mis-read an old writer's row forever. That is the
expand/contract dance plus a constraint the old writer's `INSERT` fails on, which is what `0078`
did for `PromptBody.origin`.

**Which one a reader is in, in one question:** could the value in front of it have been produced by
a newer commit of this repo than the code reading it? If no — a request body, a config file, an MCP
tool argument, a pinned third-party vocabulary — an unknown value is a bug or an attack, and
<../../STYLE.md> § General's strict data mapping applies unchanged. If yes, an unknown value is
expected, and raising on it is the defect. A third answer exists where the two ends handshake:
<../runtime/x/bridge/protocol.py> negotiates a version on its first frame and so can reject an
unknown kind outright, which is where this doctrine was already written down — for one seam.
Storage has no handshake.

## Test

```bash
bbr test //haku/console/...
```
