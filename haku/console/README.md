# haku/console — Haku's interactive console

A FastAPI service serving the trusted Haku console as a React single-page app over JSON and MCP.
The console is the operator-owned shell around Haku's cross-origin UI, the approval/audit boundary
for privileged tools, and the home of the Agent authority that admits `/mcp` callers.

It runs in its own `haku-console` namespace, outside Haku's `haku-sandbox` authority and egress
fence. That separation lets it hold credentials Haku may use only through reviewed, operator-gated
surfaces. The complete threat model and enforcement inventory are in <../docs/security.md>.

## Where each contract lives

This README is the component map, not a second copy of every contract:

- <docs/agent_authority.md> — canonical Agent identity, credentials, enrollment, profiles, and
  actor-scoped authority.
- <docs/containment.md> — iframe isolation, trusted chrome, bridge verbs, consent, and browser-side
  exfiltration bounds.
- <docs/oauth_browser_surfaces.md> — account-link and Agent-enrollment browser boundaries.
- <docs/chat_layers.md>, <docs/conversation_schema.md>, and <x/README.md> — the experimental chat
  runtime, its durable records, and channel/harness boundaries.
- <../docs/security.md> — threat model and security invariants.
- <../../cluster/k8s/haku/console/README.md> — deployment topology, migration release work, routing,
  credentials, and one-time connection bootstrap.
- <frontend/README.md> — the SPA module map and frontend build/test commands.

## The capability tier — privileged actions, operator-gated

`capabilities.py` exposes the remaining bespoke capability route,
`POST /api/capabilities/launch-routine`. It uses a console-only bearer and is protected by exact
Origin admission, a tiny reviewed allowlist, trusted-shell confirmation showing the prompt
verbatim, and audit logging in a namespace Haku cannot read. The framed Haku UI can only request a
launch through the bridge; it cannot call the route or render the deciding control.

This is transitional. `haku_routine.launch_routine` reaches the same routine through the standard
MCP approval queue. Once haku-ui uses that path, the bridge verb and bespoke capability router can
retire. There is no low-privilege console write tier: haku-ui writes its own state, while the
console's Recall mirror is read-only.

## MCP approval queue — authored tool calls, console-approved

`ToolCallApplicationService` is the actor-scoped lifecycle boundary. Agent callers enter through
`mcp_server.py`; the operator browser enters through `mcp_approval.py`. The service owns the
canonical `tool_call_id`, schema validation, reviewed auto-approval decision, audit row, waiting,
execution, and result.

Invalid arguments to an owned in-process schema are persisted born-denied and returned immediately;
lookup and discovery uncertainty fails closed to manual review. A queued call has exactly three
exits:

- the Operator approves it (`pending_approval` → `running`) or denies it (`denied`);
- the submitting Agent withdraws its own still-pending request (`withdrawn`).

Withdrawal records retraction, not human judgment and not deletion: the row persists in the audit
ledger (`GET /api/tool-calls`) as `withdrawn` with its `withdrawal_reason`, so a prompt-injected
Agent can pull an ask out of the approval queue before the Operator scrutinises it but cannot erase
that it was made. It is scoped to the canonical Agent (a sibling Agent under the same Operator sees
only `not found`), races approval under the row lock, and can only move work away from execution.
Agents never receive an approval tool. Approved execution revalidates the exact credential binding recorded at
submission, so queued work cannot transfer to a replacement credential.

The browser reads pending calls and the audit ledger through `/api/approvals/pending` and
`/api/tool-calls`; `POST /api/tool-calls/{tool_call_id}/decision` is exact-Origin-gated. The event
WebSocket is only a lossy invalidation channel: REST remains authoritative. `list_node_daemons`
reflects persisted heartbeat/lease state; the separately authenticated `/api/node-daemons/v1/*`
machine API owns heartbeat, durable work claims, lease renewal, and idempotent results. Operator
OAuth and provider associations are managed by `mcp_operator_oauth.py` and
`oauth/provider_connection.py`; browser rendering and callback-result handling are specified in
<docs/oauth_browser_surfaces.md>.

### MCP server (`/mcp`)

`mcp_server.py` mounts one native MCP server for Agents and the trusted Operator frontend. Agents
submit through `ToolCallApplicationService.submit_and_wait`. A DB-revalidated Operator session uses
`execute_direct`, resolving downstream credentials in that Operator's context without creating an
approval row; browser MCP requests still require the exact console Origin.

Discovery is request-local and actor-scoped. Shared/in-process servers plus remote servers connected
by the actor's canonical Operator are exposed in two forms:

- an unconditionally auto-approved tool is a transparent pass-through with its upstream schema;
- every other tool uses `{input, rationale, title?, wait_for_result_ms?}` and returns either the
  terminal result or a non-terminal `tool_call_id`/approval-URL stub.

`pending_approval` means the call remains queued after the requested wait; `running` means approval
occurred but downstream execution has not finished. Agents resolve either with `get_tool_call` or
`list_tool_calls`, and retract obsolete pending asks with `withdraw_tool_call`.

`call_mcp_tool(server_id, tool_name, arguments?)` is the by-name fallback for tools absent from a
client's original discovery snapshot. Its arguments are exactly the generated proxy's shape—raw
upstream arguments for pass-through tools, the approval envelope otherwise. It shares dispatch,
policy, validation, audit, and credential resolution with generated proxies, so naming a tool by
parameter cannot bypass approval.

`list_mcp_servers` is passive: configured catalog plus persisted connection state, with no token
refresh or downstream call. `get_mcp_server_status` actively resolves credentials and probes one
server, returning degraded stage/reason data instead of erasing the server. Status never includes
access/refresh tokens, client secrets, or static-bearer secret references; a configured connection
whose deploy-time client is absent reports `unprovisioned` rather than disappearing. Reflected
`approval_mode` and `input_schema` describe the caller-visible proxy shape. Upstream
`initialize.instructions` pass through rather than being restated here; tool descriptions carry the
stub semantics because many clients do not display server instructions.

Agent admission composes Haku's FastMCP OAuth adapter and configured static credentials through the
same canonical authority. An explicit invalid bearer never falls back to an ambient browser cookie.
FastMCP owns OAuth protocol machinery; Haku owns enrollment, durable authority, actor resolution,
and the Postgres-backed state required by the accepted private seam. See
<docs/agent_authority.md>.

#### Catalog reconciliation

`tools/list` is a snapshot read. `mcp_catalog_reconciler.py` builds one atomic per-Operator
generation before readiness and refreshes it periodically. Connection changes invalidate that
Operator's generation across replicas through Postgres `LISTEN`/`NOTIFY`; a newly admitted Operator
queues an immediate pass.

Successful reflection is TTL-reused and single-flighted by server/config/credential fingerprints.
Failures publish a degraded snapshot with no callable proxies. Execution never treats a catalog
snapshot as authority: it revalidates the actor binding and current credential. Upstream tool-list
changes may remain stale for one refresh interval; persistent sessions and
`notifications/tools/list_changed` are optional latency improvements, not correctness requirements.

### Canonical Agent authority and enrollment

The canonical contract is <docs/agent_authority.md>. In short: `Operator`, `Agent`, credential
bindings, grants, names, profiles, and tool-call principals are durable local identities; every
Agent call records exact binding provenance; and browser enrollment must converge with the MCP-side
principal before a binding becomes active. Access profiles independently own auto-approval,
Recall-index, in-process-server, and chat-runtime launch grants; missing assignments fail closed.
Agents submit/read only their own calls and never approve themselves.

### In-process MCP servers — no second deployment

An `mcp.servers` entry selects a remote HTTP MCP backend or a registered in-process `FastMCP`
instance. `McpServerDispatcher` uses the same client/reflection path for both, while reviewed
implementation code injects any in-process credential only at execution. Startup rejects a
credential kind the implementation did not declare.

Built-ins are assembled in `in_process_servers.py`:

- `gmail` and `google_calendar` execute as the acting Operator's separately linked Google grants.
  Their tool schemas/descriptions are the API contract; `TODO.md` inventories intentionally
  unexposed provider affordances. Auto-approval policy lives in the reviewed deployment config.
- `haku_index` searches only logical indexes granted by the Agent's access profile; direct Operator
  calls may read every configured index. In the console it is a database reader over the committed
  index state (`recall_index_reader.py`). Source materialization and embedding are the separate
  maintenance stages of `recall_index_sync.py`, run by the independently deployed `haku-indexer`
  worker (`indexer.py`) as role-flagged Deployments: one chunk Deployment per logical index, each
  mounting only its own index's config slice, plus one shared embed Deployment. Only the
  `haku-state` chunk pod holds the `haku-state` Git credential (Haku's Forgejo account, capable of
  writes but used read-only; public Ducktape is anonymous), the embed role the batch embedder
  endpoint — so this API pod carries no Git credential. <../recall_index/README.md> owns the index
  design.
- `haku_conversations` exposes actor-scoped reads over the console's chat records; the runtime and
  record vocabulary are documented under <x/README.md>.
- `haku_routine` launches the reviewed routine through ordinary approval.
- `hostexec` exchanges the acting Operator's login authority only during approved execution; its
  host-side trust boundary is documented in <../hostexec/README.md>.

The trusted frontend resolves opaque IDs by composing ordinary read tools. There are no parallel
preview-only MCP tools or HTTP routes.

## Free-form UI — Haku's own UI, embedded

The console frames `haku-ui.allegedly.works` full-page in a sandboxed cross-origin iframe and owns a
narrow rail of trusted chrome. The frame cannot read console DOM, cookies, credentials, approvals,
or capability routes. A schema-validated `postMessage` bridge carries only requests that need the
trusted side; the shell origin-checks, decides, confirms where necessary, and owns revocation.

The complete bridge and consent contract—including route/title mirroring, open-link checks,
geolocation and screenshot grants, shell-owned kill switches, and residual exfiltration bounds—is
<docs/containment.md>. Frontend ownership and routing are in <frontend/README.md>.

## Past tool calls — full-page history

`frontend/tool_calls_page.tsx` renders the Operator's durable audit ledger at
`/_console/tool-calls`. It pages newest-first by keyset cursor, defaults to 25 rows, and hides
routine auto-approved traffic unless requested. The small page is deliberate: each row may carry
whole argument/result payloads, so hundreds of rows make a multi-megabyte response. A live event
refreshes only the newest page and merges it over older pages; result/argument editors initialize
near the viewport rather than for every retained row.

Agent-facing reads are compact by default: `list_tool_calls` returns status summaries and
`get_tool_call` returns the selected result. Their `fields` selector opts into whole opaque
payloads; `get_tool_call(fields=[])` is the cheap status poll.

## Notifications — Web Push for pending approvals

Web Push reaches an Operator when no console tab is open. The server (`notifications/push.py`,
`notifications/push_routes.py`) shows one versioned notification per queued call; the service worker
(`frontend/sw.ts`) offers Approve/Deny and deep-links to the audit view. A push grants no authority:
buttons call the ordinary exact-Origin decision endpoint under the Operator session.

`PendingApprovalNotifier` updates that notification on approval, denial, or withdrawal. Calls that
never queue are never pushed. Preserve these operational contracts:

- The VAPID private key is the console's push identity; rotating it invalidates every subscription.
- Push payload changes are additive within a `kind`, because an installed service worker may lag the
  server by a day. Non-additive changes require a new variant.
- An expired one-hour Operator session turns a notification action into a re-authentication deep
  link rather than a failed decision.

Third-party notification action services are intentionally not used: they would need a deciding
credential outside the console's origin, contrary to <../docs/security.md> invariant #4.

## Perimeter / deploy

Cluster topology and operations are owned by <../../cluster/k8s/haku/console/README.md>. In
particular, that document is canonical for static/API routing, migration Jobs, rollout strategy,
OAuth/client bootstrap, connected MCP servers, credentials, and placement. Keep the high-level
boundary here: Haku cannot mutate or inspect the `haku-console` namespace; browser auth is
app-owned; `/mcp` Agent auth and Operator browser auth are separate; every new top-level backend
prefix must also be routed by the static nginx shell; and API replicas overlap during a rollout, so
stored and cross-replica contracts must tolerate adjacent releases.

### Vocabularies across a roll

A new writer meeting an old reader fails transiently: it dies with the replica. An old writer
meeting a new reader fails permanently: it dies with the row. Tolerance fixes the first; only a
constraint fixes the second.

**Readers tolerate narration and cross-replica payloads produced by a newer replica.** Those values
must not raise merely because an older reader has no word for them. They decode to a named
unknown—such as `util.sqlalchemy_types.UnknownValue` or `conversation/conversation_event.UnknownEventBody`—never
`None` or a nearby member, so each consumer must handle the uncertainty explicitly. Cross-replica
payload models do not reject unknown fields.

**Writer rollout depends on the vocabulary:**

- **Narration** is append-only information a reader may correctly skip, such as session events,
  notification kinds, and `ConsoleEvent.event_type`. A new value may ship with its writer in one
  release; the skipped narration is the named compatibility cost.
- **Decision** values drive behavior, such as session, turn, tool-call, provenance, or rejection
  statuses. No old-reader guess is safe, so a reader that knows the new concrete member ships one
  release ahead of the writer and the writer waits for convergence. Decision columns are currently
  strict—they do not decode unknown values to `UnknownValue`. Tolerant decoding could keep an
  unrelated inventory read alive, but it would not remove the two-release rule because no consumer
  may guess what the value means.
- **A required field added to an existing shape** is a narrowing, not a vocabulary extension. Use
  expand/contract plus a constraint that makes the old writer fail instead of silently creating a
  permanently misread row.

The deciding question is: could this value have been produced by a newer commit than the reader?
If no—a request body, config file, MCP argument, or pinned third-party vocabulary—an unknown value
is a bug or attack and root <../../STYLE.md> strict mapping applies. If yes, unknown data is expected
and raising is the defect. A version-negotiated seam, such as <../runtime/x/bridge/protocol.py>, may
reject unknown kinds after its handshake; storage has no handshake.

## Test

```bash
bbr test //haku/console/...
```
