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

- **CSRF-gated.** A header-located double-submit token: the SPA fetches it from
  `GET /api/capabilities/csrf` (which also sets the signed cookie) and echoes it in
  `X-CSRF-Token` on the POST — so a cross-site request can't ride the operator's
  Authentik session cookie to fire a capability.
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
fires the same routine, gated by the ordinary approval drawer instead of a separate confirm.
This capability path stays only until haku-ui submits `launch_routine` through its backend and
the `requestLaunch` bridge verb is dropped, at which point `capabilities.py` retires entirely
(the shared `GET /api/capabilities/csrf` endpoint moves; it's used by the approval + operator-auth
flows too).

There is **no** low-privilege "trace" write tier anymore — operator feedback now writes
straight into haku-state from haku-ui (which Haku already owns), so the console needs no
haku-state git credential or clone at all.

## MCP approval queue — authored tool calls, console-approved

The console also owns the privileged MCP-tool escape hatch (`mcp_approval.py`). Haku or haku-ui can
submit a precise tool call; the console mints the canonical `tool_call_id`, records the audit entry,
runs the reviewed auto-approval decision, asks the trusted console frontend for approval when it
does not match, executes the MCP tool, and keeps the result. The decision validates arguments
against the existing FastMCP tool's generated schema; its audit-safe evaluation string is recorded
even when the call stays manual, while errors are logged and fail closed. haku-state stores only authored requests
(`tool_requests/*.yaml`) and UI affordances (`<tool-call request="...">`); there is no
`tool_results/` mirror.

Core endpoints:

- `GET /api/capabilities/mcp-servers` — reflect the configured connected MCP servers and each
  server's `tools/list` metadata. Entries are explicitly `alive` or `degraded`; the console config
  names reachable servers, and each live MCP server remains the tool schema source.
- `GET /api/mcp/operator-auth`, `POST /api/mcp/operator-auth/{server_id}/connect`,
  `DELETE /api/mcp/operator-auth/{server_id}`, and `GET /api/mcp/operator-auth/callback` —
  operator account association for MCP servers whose config enables `operator_oauth` (this flow
  lives in `mcp_operator_oauth.py`, not the approval router). The catalog stays in the console
  YAML/ConfigMap; Postgres stores only short-lived DCR/PKCE flow state and per-operator token
  associations.
  Connecting is available only while unconnected; disconnect first to replace an account link.
- `POST /api/tool-calls` — submit a call with `server_id`, `tool_name`, exact
  `arguments`, and explicit `wait_for_ms`. The console mints the canonical `tool_call_id`.
- `GET /api/approvals/pending`, `GET /api/approvals/events?after_event_id=...`, and
  `WebSocket /api/approvals/ws` — frontend catch-up + notifications. REST remains the source of
  truth; the WebSocket only wakes the shell to refresh.
- `POST /api/tool-calls/{tool_call_id}/decision` — CSRF-gated trusted-frontend approval/denial.
- `GET /api/tool-calls` / `GET /api/tool-calls/{tool_call_id}` — audit/result reads for Haku's
  normal sweep or operator debugging. The list endpoint accepts repeated `status` filters and a
  datetime `since` filter on `updated_at`.

Backend callers authenticate with a configured **static agent** bearer (`static_agents` in the
config file — each an agent id bound to one operator subject, both env-referenced). Browser-origin
approvals use the operator's Authentik session plus CSRF. The approval drawer renders in trusted
console chrome, not inside Haku's iframe, and does not block the framed Haku UI. If a server enables
`operator_oauth`, approval execution
uses the approving operator's linked OAuth token and refuses to move the call out of
`pending_approval` until that association exists. Static bearer credentials can remain configured
for reflection or fallback wiring, but they are not silently substituted for operator-approved
execution on an `operator_oauth` server.

### Agent-facing MCP server (`/mcp`)

`mcp_server.py` mounts a native MCP server at `/mcp` so a connected Claude client (the claude.ai
custom connector / the `claude` CLI) can call the connected-server tools directly — the same
submit → auto-approve → execute → wait path as `POST /api/tool-calls` (the shared `submit_and_wait`
helper), never a divergent one. The surface is two buckets: tools the policy **unconditionally**
auto-approves (gmail reads, read-only grocy-sf, tana `get_or_create_calendar_node`) appear as
transparent **pass-throughs** (original name/schema, real result); everything else appears as
`request_<server>_<tool>` with an envelope `{input, title?, rationale, wait_for_approval_ms?}` that
returns the real result if approved within the wait, else a **promise** (a pending `tool_call_id` +
an operator-facing deep-link `url`) the agent resolves via the `get_tool_call` / `list_tool_calls`
read tools. The promise-semantics preamble lives in each tool's **description** (many MCP clients,
claude.ai included, never surface a server's `instructions`). Auth is FastMCP `MultiAuth`: an
Authentik-backed `OIDCProxy` (DCR + PKCE for claude.ai / `claude`, DCR/token state persisted in the
console's Postgres) composed with the configured `static_agents`' fixed bearers. The `/mcp` surface only submits/reads — there
is no decision tool — so an OAuth caller cannot self-approve; approval stays in trusted console
chrome. Approved calls execute against the console's own stored operator credentials, so an incoming
token's blast radius is "call the console's submit/read tools" and nothing else.

### In-process MCP servers — no second deployment

A `mcp.servers` entry that omits `server_url` is served by an **in-process `FastMCP`
instance** instead of a remote server reached over the network: `fastmcp.client.Client`
accepts a `FastMCP` object directly (an in-memory `FastMCPTransport`), so
`McpToolExecutor`/`McpMetadataProvider` run the exact same `Client(...)` calls either
way — same approval/audit/CSRF pipeline, same live `tools/list` reflection, just a
different transport (`_transport()` in `mcp_approval.py` picks the registered in-process
`FastMCP` for a server id, falling back to `server_url`). There are three today, built like
standalone MCP servers from `@mcp.tool`-decorated functions. The only transport difference is that
`create_app` hands the `FastMCP` object straight to the executor instead of serving it over
HTTP:

- **`gmail`** (`haku.console.tools.gmail`). Reads mirror Gmail's REST API and return its
  resource shapes **verbatim** (`gmail_api.messages`/`gmail_api.labels`) — no content-type
  smartness, no body decoding, no flattening: `threads_list` (paginated via
  `page_token`/`next_page_token`), `threads_get`/`messages_get` (with a `format` argument that
  passes straight through to Gmail — `minimal`/`metadata`/`full`, plus `raw` for messages),
  `labels_list`, `labels_get`. Writes are `drafts_create`, `threads_modify_labels`, and
  label CRUD (`labels_create`, `labels_patch`, `labels_delete`). Calls default to operator approval.
  The reviewed `v1` decision auto-approves Haku-agent calls to every
  read tool above, plus `threads_modify_labels` when every added/removed name starts with `haku/`,
  `labels_patch` only when both the current and new names start with `haku/` and no visibility is
  changed, and `labels_delete` only when the current label name starts with `haku/`. Patch/delete
  resolve the submitted label ID before deciding. Gmail affordances not
  yet exposed (send, trash/delete, message-level modify, drafts list/send, attachments,
  history, settings/filters) are tracked in `haku/console/TODO.md`.
- **`google_calendar`** (`haku.console.tools.google_calendar`): `create_calendar_event`, plus a
  `GET /api/google-calendar/calendar-summary` rendering read (below).
- **`haku_routine`** (`haku.console.tools.routine`): `launch_routine` fires the Haku
  claude-code-web routine (optionally with per-run instruction `text`), so a launch is an
  ordinary approval-gated tool call rather than a bespoke capability. It uses the
  `haku-routine-launch-token` secret (`HAKU_CONSOLE_LAUNCH_ROUTINE__*`), not the Google grant, and
  supersedes the launch-routine capability tier above (kept during the haku-ui transition).

The `gmail` and `google_calendar` servers are built from **one** Airlock-issued `haku_console_google` token
(`calendar.events` + `gmail.modify` + `gmail.compose`, plus the `google` provider's read-only
scopes), mounted from `haku-console-google-access-token` (`HAKU_CONSOLE_GOOGLE_TOKEN_DIR`) —
kept separate from every other Google-scoped credential in the cluster and delivered only to this namespace.
One-time operator OAuth bootstrap and the scope list: `cluster/k8s/haku/console/README.md`.
Two plain HTTP endpoints alongside the MCP tools render approvals whose tool call carries only
opaque ids: `GET /api/gmail/thread-previews` (subject/snippet/current-labels for a pending
`threads_modify_labels`) and `GET /api/google-calendar/calendar-summary` (a non-primary
`calendar_id` → the calendar's display name + a Google Calendar link, for a pending
`create_calendar_event`). Both stay outside `build_mcp`'s tool surface since they're reads for
rendering, not something Haku calls.

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
schema-validates, and decides/confirms before acting. It also mirrors the iframe's hash route
(`routeChanged`, validated as a path) into the console's own URL fragment so refresh and deep
links restore the view. A persistent top-right floating toolbar (`console_panel.tsx`'s
`ShellChrome`, each button `filled` while its panel is open) opens the shell's own trusted
chrome: a checklist button — badged with a callout light when a tool call is awaiting approval —
toggles the approval queue panel (with a link to the full-page past-tool-calls history); a gear
toggles the Settings panel (MCP account connect/reconnect/disconnect); a location-sharing pin
(shown only while consent is held, with a live indicator when location is actively read) toggles
a stop/withdraw panel; and a crossed-wifi button appears when the live event socket is down.
Opening more than one panel stacks them vertically under the toolbar rather than overlapping.
See <docs/containment.md>.

## Past tool calls — full-page history

Beyond the drawer's ephemeral "Recent" list, the console owns a **full-page history view**
of the whole tool-call audit ledger (`frontend/tool_calls_page.tsx`), reached from the
console panel and living at its own route, `/tool-calls` (`frontend/routing.ts`). Because
the console's own pages are distinguished by URL **path** — the hash stays reserved for
mirroring the framed haku-ui route — the shell renders the history view instead of the
iframe when the path matches. It reads `GET /api/tool-calls?newest_first=true`, so the
newest calls survive the query's limit. Production's nginx already serves the SPA for any
non-asset/API path; `app.py`'s dev fallback mirrors that so deep links work locally too.

## Layout

| Path                         | Role                                                                                                                                                                                                                                |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app.py`                     | FastAPI `create_app`. `GET /api/config`, `GET /healthz`, CSRF config, mounts the capability router. It can serve the SPA for local/direct fallback when `HAKU_CONSOLE_STATIC_DIR` is set.                                           |
| `capabilities.py`            | Capability-tier router (`/api/capabilities/*`): CSRF-gated, audited privileged actions. `POST /launch-routine` fires the routine with the server-side bearer and optional per-run text; `GET /csrf` issues the double-submit token. |
| `mcp_approval.py`            | MCP approval queue router: MCP server reflection, tool-call submit/list/result endpoints, trusted approval decisions, WebSocket notifications, and Postgres-backed audit state in deploy.                                           |
| `mcp_config.py`              | The connected-MCP-server catalog (deploy-time YAML model) plus how to reach each entry: in-process/remote transport and static bearer credential. Shared by `mcp_approval` and `mcp_operator_oauth`.                                |
| `in_process_servers.py`      | Canonical builder catalog for the Gmail, Google Calendar, and routine FastMCP servers, shared by the production app and schema exporter.                                                                                            |
| `mcp_operator_oauth.py`      | Operator OAuth account linkage for servers that execute as the operator's own account: the DCR/PKCE flow, Postgres token storage/refresh, and the `/api/mcp/operator-auth/*` connect/disconnect/callback endpoints.                 |
| `migrations/`                | Alembic migrations for the deployed haku-console database; the console applies them at app startup before serving the API.                                                                                                          |
| `models.py`                  | Pydantic `ConfigResponse` — the `/api/config` response model.                                                                                                                                                                       |
| `config.py`                  | Env settings (`HAKU_CONSOLE_*`).                                                                                                                                                                                                    |
| `export_schema.py`           | Prints the OpenAPI schema; the frontend generates its TypeScript types from it.                                                                                                                                                     |
| `export_mcp_tool_schemas.py` | Reflects the real in-process servers through MCP `tools/list` and prints their exact input schemas for generated frontend validators and types.                                                                                     |
| `frontend/`                  | React SPA (esbuild bundle) — see `frontend/README.md`.                                                                                                                                                                              |

## Perimeter / deploy

Manifests live in `cluster/k8s/haku/console/` (operator-owned — the perimeter is not
Haku's to change); the `haku-console` namespace itself is `cluster/k8s/haku/console-namespace/`.
The deployment runs two containers in one pod: the `haku-console` FastAPI API
image and a separate `haku-console-static` nginx image that bakes in the
fingerprinted SPA. nginx serves `/` and `/assets/*`, proxies `/api/*`, `/healthz`,
`/mcp`, `/auth/*`, and the `/.well-known/oauth-*` discovery paths to FastAPI on
localhost, and sets cache policy by route (`/assets/*` immutable, app shell
revalidated, API/health/mcp/auth uncached). No runtime asset copy or shared web
volume is used.

**Auth is app-owned** (the Authentik forward-auth proxy outpost that used to gate
`haku.allegedly.works` is retired; the HTTPRoute now points straight at the console
Service). The operator browser logs in via Authentik OIDC (`HAKU_CONSOLE_OPERATOR_OIDC__*`
→ signed session cookie; router-level dependency guards protect `/api/*`, with the agent bearer
scoped to the agent-facing submit/read routes only), and agents authenticate to the always-mounted
`/mcp` via `MultiAuth` (OIDCProxy DCR + the `static_agents`' fixed bearers, `HAKU_CONSOLE_MCP_OAUTH__*`;
the DCR/token state persists in the console's own Postgres via `HAKU_CONSOLE_MCP_OAUTH_PERSISTENCE__*`).
The console refuses to start with no `/mcp` credential at all (no static agent and no `mcp_oauth`).
Both OAuth2 providers and their client secrets
(the `haku-console-oidc` Secret) are minted by `tf/gitops/agent-machine-access`; single-user
access is Authentik's application access policy.

Postgres is **required**: it backs the approval ledger and the operator OAuth token store, and the
console applies its Alembic migrations once at startup (`app.main`, before serving) — never as a
side effect of constructing a store.

Non-root, dropped caps, no service-account token. Credentials: the
`haku-routine-launch-token` secret (the launch capability bearer; `HAKU_CONSOLE_LAUNCH_ROUTINE__TOKEN`),
the OAuth client secrets (`haku-console-oidc`), the config-file/database settings
(`HAKU_CONSOLE_CONFIG_FILE`, `HAKU_CONSOLE_DATABASE_URL`, `HAKU_CONSOLE_PUBLIC_BASE_URL` — the OAuth
redirect-URI origin), and each `static_agents` entry's env-referenced bearer + operator subject
(e.g. `HAKU_CONSOLE_AGENT_HAKU_TOKEN` from `haku-console-agent-api`, `HAKU_CONSOLE_AGENT_HAKU_OPERATOR`
from the TF-fed `haku-console-oidc` `operator_subject`).
It no longer holds a haku-state git credential — feedback/trace writes moved into haku-ui.
As trusted ducktape code in its own namespace it is **not** behind the `haku-egress-proxy`
fence — it gets ordinary cluster egress (which the capability tier needs to reach the
Anthropic fire URL). Security model: `haku/docs/security.md`; roadmap: `haku/PLAN.md` and the
`haku-state` repo's `plans/dashboard-arm.md`.

## Test

```bash
bbr test //haku/console/...
```
