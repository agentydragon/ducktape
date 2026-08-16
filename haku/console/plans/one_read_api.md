# One read API for sessions — where the line actually falls

The console reads its session/conversation corpus through two surfaces that answer nearly the
same questions off the same tables:

| Question                         | REST                                 | MCP (`haku_conversations`)   |
| -------------------------------- | ------------------------------------ | ---------------------------- |
| which sessions are there         | `GET /api/conversations`             | `list_conversations`         |
| what was said (interpreted)      | `GET /api/conversations/{id}`        | `read_transcript`            |
| the raw protocol log             | `GET /api/conversations/{id}/frames` | `read_rollout`, `read_frame` |
| the exchanges and what they cost | (nested in the detail view)          | `list_turns`                 |

The proposal on the table: the trusted frontend already holds an Operator session on `/mcp`, so
let it call these tools as its Operator and delete the REST twin.

## Recommendation

**Do not move the conversation reads onto MCP. Make the _store_ singular, not the API — that is
where the hand-syncing actually happens — and take the socket/read split for live updates.**

The boundary, stated as a rule:

- **REST owns every read whose answer depends on _who is asking_** — the inventory
  (`/api/conversations`), the interpreted transcript (`/api/conversations/{id}`), and the frame
  inspector (`/api/conversations/{id}/frames`). An in-process MCP server is handed a
  **credential, never a caller** (below), so operator scoping is not expressible there today.
- **MCP owns every read whose answer is the same for everybody with the authority to ask it** —
  reflection (`list_mcp_servers`, `get_mcp_server_status`, `list_node_daemons`, already MCP-only)
  and semantic recall (`haku_index.search`), which has no REST twin and should never grow one.
- **`/api/events/ws` says _what changed_; the read surface says _what it is_.** A
  `SessionChangedEvent {session_id}` is an invalidation, not a payload. The SSE stream
  (`/api/sessions/{id}/stream`) is a third mechanism that duplicates the socket, not MCP, and
  should retire into it.
- **Mutations, the WebSocket, and anything the service worker touches stay REST**, always.

The duplication the operator correctly noticed is real, but it is **two Pydantic families over one
query**, not two implementations. `SessionStore.list_turns` already serves both `list_turns` (MCP)
and the `turns` array of the REST detail view; `_frames_of_kinds` already gives `read_frames` (MCP)
and `read_operator_frames` (REST) one delta-exclusion policy. Deduplicating models and store methods
costs one small PR and no semantics; deduplicating transports costs a new credential kind, an
argument shape that is downstream of an agent policy file, and a scoping premise the repo has
already voted to retire.

## 1. The prior art is real — and it is narrower than it looks

The trusted frontend does enter `/mcp` as its current Operator, and five modules already depend on
it.

**Admission.** `_OperatorMcpSessionAuthenticator` in <../mcp_agent_auth.py> accepts a request on the
exact `/mcp` path when it carries a DB-revalidated operator session cookie
(`operator_session_for_identity_store`, which re-resolves the identity in Postgres on every call)
**and** an `Origin` header exactly equal to `settings.public_base_url` — otherwise it raises
`OperatorSessionAuthenticationError`. That is the same exact-Origin rule
`require_operator_mutation_origin` applies to REST mutations, applied to every browser MCP request
including reads: the same-site but untrusted haku-ui iframe cannot ride the cookie.

**The client.** <../frontend/mcp_client.ts> holds one lazily-connected
`Client` + `StreamableHTTPClientTransport` against `/mcp` with `credentials: "same-origin"`, and
wraps `fetch` so a `401` calls `redirectToOperatorLogin()` — the MCP path's copy of the same 401
bounce `client.ts` installs as `openapi-fetch` middleware. `callOperatorMcpTool(name, args)` calls
the tool, runs `mcpToolError` (joins the `isError` result's text blocks) and throws, else returns
`unwrapMcpToolResult` (selects `structuredContent`, undoing FastMCP's `{result: …}` envelope when
`_meta.fastmcp.wrap_result` says so). **Any** throw nulls `connectedClient`, so the next call
re-handshakes.

**Execution.** For an Operator principal `mcp_server.py` skips the lifecycle entirely:
`ToolCallApplicationService.execute_direct` resolves the backend credential for that Operator,
logs `operator direct MCP call server=… tool=… operator_id=…`, and dispatches. No tool-call row, no
approval event, no non-terminal stub — but also more audit logging than the REST route it would
replace, which logs nothing per read.

**The call sites, and how they actually behave:**

- <../frontend/mcp_status_client.ts> — Settings reads `list_mcp_servers`,
  `get_mcp_server_status`, `list_node_daemons` and `.parse()`s each against a generated schema.
  These are console-native tools with no REST twin at all.
- <../frontend/gmail_client.ts> — resolves an opaque thread/message id into a subject and
  snippet by composing `gmail__labels_list` with `gmail__threads_get`. Failure is per-id
  `console.warn` + `null`, and the widget falls back to showing the raw id.
- <../frontend/calendar_client.ts> — `google_calendar__list_events` with `max_results: 1` to
  turn a calendar id into its `summary`. This one does **not** catch: the caller sees the throw.
- <../frontend/grocy_client.ts> — remote server, hand-authored Zod (its OpenAPI tools are outside
  the generated catalog).
- <../frontend/tana_client.ts> — and this is the one worth reading before adopting the pattern
  more widely:

  ```ts
  // read_node is globally approval-shaped for Agent callers. The Operator branch accepts the
  // same advertised envelope but executes its `input` directly without creating a row.
  const payload = await callOperatorMcpTool("tana_rw__read_node", {
    input: { nodeId, maxDepth: 0 },
    rationale: "Resolve a Tana node name for an operator approval preview",
  });
  ```

  The browser has to send the approval **envelope** and invent a `rationale` string for a render,
  because `read_node` is not in an unconditional auto-approval policy. Whether a console page sends
  raw arguments or `{input, rationale}` is decided by
  <../../../cluster/k8s/haku/console/config.yaml> — see §5.

So the prior art is: **trusted renderers resolving reference data on third-party servers**, plus the
console's own reflection tools. It is not yet a page's primary data source, and no call site has to
be operator-scoped — a Gmail thread is scoped by the credential, which _is_ the Operator's.

## 2. The generated types — what exists, and what a session reader would get

<../export_mcp_tool_schemas.py> builds the real FastMCP servers with inert collaborators, drives
them through an in-memory `Client`'s `tools/list`, inlines `$ref`s, unwraps FastMCP's
`x-fastmcp-wrap-result` envelope, and validates every schema against `_FRONTEND_SCHEMA_KEYWORDS` —
a deliberately narrow allowlist, so a construct the frontend's `z.fromJSONSchema` adapter has not
been reviewed against fails generation rather than silently weakening a validator. It emits two
catalogs (`--results` selects the second), and `js_json_schema` in <../frontend/BUILD.bazel> turns
each into a `.schema.json` plus a `.d.ts`. <../frontend/mcp_tool_result_schema.ts> compiles the
whole result catalog eagerly at module load.

**A session reader would _not_ get this for free today — but nearly.** `build_schema_servers()`
constructs `InProcessServerDependencies(routine_launcher=…, hostexec=…)` and leaves `rollout` and
`index` unset, and <../in_process_servers.py> registers `haku_conversations` only
`if dependencies.rollout is not None`. So the five conversation tools are absent from both
catalogs.

The fix is one argument. `conversations_tools.build_mcp(reader)` only closes over the reader; no
`@mcp.tool` body touches it until execution, which is exactly the invariant `_InertCollaborator`
exists to prove. Passing the same inert object as `rollout=` (and `index=`) registers both servers
for reflection. Every JSON Schema keyword those models produce is already in the reviewed
allowlist — `anyOf`/`type`/`format` (`uuid`, `date-time`), `additionalProperties` for
`payload: dict[str, Any] | None` and `usage`, `enum` for the `FrameKind` literal union, `items`,
`minimum`/`maximum` from the `ge`/`le` fields, `description`, `default`, `title` — and
`list_conversations`' `-> ConversationPage` return is an ordinary object result, like
`read_rollout`'s `RolloutPage`. So this should generate without adapter work,
and if it does not, that is a cheap and decisive answer (§7, Stage 1).

## 3. The gap list, endpoint by endpoint

### `GET /api/conversations` vs `list_conversations`

|         | REST                                            | MCP                                              |
| ------- | ----------------------------------------------- | ------------------------------------------------ |
| scope   | `WHERE Session.operator_id = actor.operator_id` | none — every session, every room, every Operator |
| order   | `updated_at DESC, session_id DESC`              | `created_at DESC`                                |
| paging  | `limit` 1–100, no cursor                        | `limit` 1–100, keyset on `(created_at, id)`      |
| payload | `+ updated_at, message_count, last_message_at`  | no aggregates                                    |

Two gaps, and only one is closable. The aggregates are a `COUNT`/`MAX` join the agent surface has
no use for; adding them is a React page shaping an LLM-facing tool. The scope gap is structural
(§4). `error` is on **both** payloads and is not a difference. The ordering columns differ, which
is a real one: "newest" means last touched on the REST side and first created on the MCP side, and
a session that is still running sorts differently under each.

### `GET /api/conversations/{id}` vs `read_transcript`

**A twin arrived after this was written (#4145), and it is not the same answer.**
`get_operator_conversation` returns the **interpreted** view: the transcript (`SessionMessageView`,
with `tool_calls` joined to their results out of the rollout and, since #4105,
`source_first_frame_seq`/`source_last_frame_seq`), the setup narration, and the turn summaries —
assembled in one call, from `session_messages`. `read_transcript` pages the **projection**: the
frame log folded through `claude_code/projection.py` into neutral entries, computed per read and stored
nowhere.

So they read different sources and answer different questions, and the duplication is not the kind
§7 could delete. The REST view is the console's own product-shaped interpretation of rows;
`read_transcript` is what the frame log means, which is the surface an agent should get and a
browser page should not depend on while the fold is still landing
(<../../plans/chat_runtime_projection.md> § stage 4). The two converge when the projection has rows
of its own — at which point this section is worth re-reading rather than re-deriving.

### `GET /api/conversations/{id}/frames` vs `read_rollout` + `read_frame`

The genuine duplicate — same table, same rows, one shared delta-exclusion helper. But four
differences, each deliberate and recorded:

- **Direction.** `read_frames` pages **forward** on `frame_seq > after_seq`;
  `read_operator_frames` pages **backwards** on `frame_seq < before_seq` and reverses each page,
  because the frames an operator opens this for are the session's last ones. `next_after_seq` and
  `next_before_seq` are opposite cursors over the same keyset, and neither is derivable from the
  other without a count.
- **Clipping.** MCP spends a `MAX_PAGE_BYTES = 200_000` budget per page and hands back
  `clipped_bytes` with no `payload` for a frame that alone exceeds it — a **context** budget, for a
  model. `SessionFrameView.payload` is a required `dict`, because clipping a frame on the surface
  that exists to appeal a lossy projection would be that projection one level down. A browser on
  MCP would have to handle `clipped_bytes` plus a second `read_frame` round trip that REST never
  needs.
- **Scope.** REST checks `Session.operator_id` before reading a row. MCP does not, by design.
- **Types.** `SessionFrameView.direction` is the `FrameDirection` enum; `RolloutFrame.direction`
  is `str`, and its `payload` is `dict | None`. The frontend would lose a discriminated field and
  gain a nullable one.

### `list_turns` — the one that is already singular

`ConversationTurnView` is `TurnRecord` minus `first_frame_seq`/`last_frame_seq`, and both come out
of the same `SessionStore.list_turns`. The REST model's docstring says "without exposing the raw
frame range yet" — but with the frame inspector merged, that range is precisely the link from a
turn to its frames. **This is the one place to actually delete a model**, and doing so needs no
MCP at all.

### Cost per request

An in-process MCP read is not free relative to the REST route:

1. FastMCP is mounted `stateless_http=True` (<../app.py>), so every request is a fresh protocol
   request.
2. `OperatorToolProvider._get_tool` runs on `tools/call` as well as `tools/list`: it resolves the
   actor, finds the server for the `<server>__<tool>` name, and **reflects that server** —
   an in-memory `Client` handshake plus `tools/list` — reusable for
   `mcp_catalog_cache_ttl_seconds` (default 60) keyed by config + credential fingerprint.
3. `McpServerDispatcher.execute` then opens **a second** in-memory `Client`, calls the tool, and
   tears it down.

Both paths pay the same DB session revalidation, so the delta is one cached reflection plus one
in-memory handshake and the protocol envelope — small per call, but paid **per tool call**, where
REST composes server-side (the detail view answers transcript + turns + narration in one request).
This console has already been bitten by per-read overhead: pods are pinned to `hil-ovh` because "an
operator API call opens a database session per read and each session costs several round trips",
turning a 4.6 ms query into a two-second request (<../README.md> § Perimeter / deploy). A page that
fans three MCP reads where it used to make one REST call is moving in the wrong direction.

## 4. The structural blocker: an in-process server sees a credential, never a caller

`McpServerDispatcher.execute` calls `_transport(server, in_process, auth_token)`, and for an
`InProcessBackend` that is `registration.builder(auth_token)` — a `str | None` and nothing else.
The acting Operator never crosses into the in-process server. `gmail` and `google_calendar` appear
operator-scoped only because their credential _is_ per-Operator (`InProcessCredentialKind.OPERATOR_CONNECTION`
resolves that Operator's Google token, and Google does the scoping). `haku_conversations` is
`credential: none`, so it receives nothing about the caller at all.

So "make the MCP conversation reads operator-scoped" is not a policy tweak. It is a fourth
`InProcessCredentialKind` that carries the acting Operator's identity into the builder, plus a
per-Operator server instance, plus a decision about what that means for an Agent caller — the exact
machinery R5.3a said not to build.

And the premise that makes an unscoped read harmless today is **already retired on paper**.
<../../plans/matrix*chat_runtime.md> R5.3a ("one operator, one Haku and one room, so the fence would
separate Haku from its own history and nothing else") carries a note dated 2026-08-15: \_superseded,
on exactly the condition this recorded* — reads become **tier-scoped**, with "a decision function
at one console call site, not scoping smeared through the transport". <../../state_index/README.md>
§ Read scoping says the same thing about the index: "the moment a second operator or a room Haku
should not see exists, ranked retrieval is where that leaks first".

Building a browser page on the unscoped reader would therefore be building on a premise the repo has
decided to retire — and would make the console a second consumer of the decision function that does
not exist yet.

## 5. The honest counter-argument

Beyond scoping and cost, three costs a plan should state rather than discover.

**The browser's argument shape is downstream of an agent policy file.** `_is_passthrough` asks
`AutoApprovalPolicyRegistry.tool_mode`, and for an `OperatorActor` that is the `max` over _every_
assigned policy root. The five `haku_conversations` tools present pass-through to the console only
because `haku_recall_reads` in <../../../cluster/k8s/haku/console/config.yaml> lists them as
`exact_tools` for **Haku**. Drop `read_rollout` from that policy — a change about what an agent may
do unsupervised — and the console page silently starts needing `{input, rationale}`, which is
exactly the shape `tana_client.ts` already carries. Coupling a trusted page's wire format to an
agent's standing-authority config is a load-bearing surprise.

**The MCP surface is agent-facing, and its prose is its interface.** <../tools/conversations.py> is
written for an LLM reader: "Context is the scarce resource", "worth doing only to see how far an
answer got before it was cut off", a `kinds` description that explains why `stream_event` must be
asked for by name. `MAX_PAGE_BYTES` exists to protect a model's context. Reshaping any of it around
a React page's pagination or its need for whole payloads makes the tool worse at its first job, and
the pressure would be constant because the page is the surface with a human complaining about it.

**Fewer, worse errors.** REST returns FastAPI's `{detail: …}` and `errorDetail` surfaces that exact
string; an MCP failure arrives as joined text blocks from an `isError` result. A page moving to MCP
trades a specific, typed 404/409 for a message blob — and loses the generated `paths` typing that
makes `api.GET("/api/conversations/{session_id}/frames", …)` a compile-time contract, in exchange
for a generated _result_ type only.

**What MCP genuinely buys, so the trade is visible:** one description of a read instead of two; a
result type the frontend validates at runtime from the Python model (the pattern Settings, Gmail
and Calendar already use); no new route, view model or OpenAPI entry per question; and an audit log
line per operator read that REST does not write.

## 6. What must stay REST, and why

- **Every mutation.** `POST /api/tool-calls/{id}/decision`, `/api/capabilities/launch-routine`,
  session create/abort/messages/delete, `/api/push/*`, operator-connection connect/disconnect.
  These are exact-Origin-gated operator gestures against trusted chrome; routing them through
  `execute_direct` would mean a browser executing an unapproved mutating tool, which is the one
  authority the approval queue exists to hold. `withdraw_tool_call` is the single console-native
  ledger mutation on `/mcp` and it is deliberately agent-only.
- **`/api/events/ws`.** `stateless_http=True` means there is no MCP session for the server to push
  over, so server-initiated notifications are unavailable on `/mcp` **as deployed** — a deployment
  choice with a stated reason (<../mcp_reflection_cache.py>), not a law. Changing it would mean
  persistent sessions, which <../README.md> § Catalog reuse already flags as the natural next step
  for an unrelated reason (upstream `notifications/tools/list_changed`). If that ever lands it is a
  consequence, never a prerequisite for anything here. Meanwhile the socket is already
  operator-scoped, fans out across replicas via Postgres `LISTEN/NOTIFY`, and has a worked-out
  expiry story (close code `4001` → the shell re-authenticates).
- **`/api/sessions/{id}/stream` (SSE).** A third mechanism, and the odd one out: it re-serializes
  the entire `SessionView` on every wake and compares it against the last payload to suppress
  no-ops. It duplicates the WebSocket, not MCP. It should collapse into the socket
  (`SessionChangedEvent {session_id}` + a re-read), which also gets it cross-replica fan-out and the
  `4001` expiry handling for free.
- **The service worker.** <../frontend/sw.ts> posts decisions to
  `/api/tool-calls/{id}/decision` from a context with no MCP client, and treats a 401 as expected
  by opening the console at the call's deep link. Putting an MCP SDK in a service worker to answer a
  notification tap is not a trade worth making.
- **`/api/node-daemons/v1/*`.** Separately authenticated machine API, deliberately outside browser
  Operator auth.
- **`/api/config`, `/api/deployment`, `/auth/*`.** Shell bootstrap; no agent-facing question.

### The division to build toward

- **The socket says what changed** — `SessionChangedEvent {session_id}`, an invalidation and not a
  payload.
- **The read surface says what it is** — the fetch that follows.

Both are keyed by `session_id`, which is what lets an invalidation name a thing the reader can then
fetch. Note that this works whichever surface wins the read: it is orthogonal to the MCP question,
which is why it is worth doing first.

## 7. Sequencing

Each stage is independently mergeable, independently revertible, and small enough to review in one
sitting.

### Stage 1 — make the reader reflectable (the cheapest experiment)

One file, zero production behavior: pass an inert reader as `rollout=` (and `index=`) in
`build_schema_servers()` so `haku_conversations` and `haku_index` enter both generated catalogs.
Nothing calls the generated types yet.

**This is the experiment that proves or kills the idea**, because it decides the half of the
proposal that is pure upside ("even getting autogened types") at the cost of one small review. Kill
criterion: `_validate_frontend_schema` or `z.fromJSONSchema` rejects a published schema, or
`mcp_tool_result_schema.ts` fails to compile the catalog — in which case the frontend would have to
hand-author Zod for every session read and the whole proposal loses its main advantage.
Revert: one file.

### Stage 2 — delete the duplicated model, not the route

Drop `ConversationTurnView` and return the frame range that `TurnRecord` already carries, so the
detail view's turns link to the frame inspector. One store method, one model, both surfaces.
This is the actual duplication, and removing it needs no transport decision.

### Stage 3 — the socket/read split — half landed

`SessionChangedEvent {session_id}` is in the `ConsoleEvent` union and the conversations list and
detail pages re-read on it (#4132, `x/session_live_updates.py`, coalesced per session). **What is
left is the deletion**: `/api/sessions/{id}/stream` is still mounted and `/chat` still holds it, so
the console has two live-update mechanisms rather than one. Retiring it is gated on the page merge
(<session_channels.md> § 2), which is where a coalesced refetch has to prove itself against the SSE
path it replaces — and it is what makes the `asyncio.wait` abort dance in `_run_turn` removable.
The split itself was valuable whatever §7's later stages decide, and it still is.

### Stage 4 — one page, one tool, on purpose

**Only worth doing if the operator wants the MCP read path exercised for real:** a search box on the
conversations list calling `haku_index__search` through the existing operator MCP transport. It is
the only session-corpus read with no REST twin, it is already pass-through for the Operator via
`haku_recall_reads`, and it needs no new route, no new credential kind, and no new scoping decision.

It does inherit one debt, stated plainly: the index is unscoped across rooms and operators, so this
page is correct only while there is one Operator. Tie it to the same gate as everything else — the
`tier` column on `sessions` from <../../plans/information_trust_tiers.md> — rather than treating it
as a fresh decision. If that debt is unwanted, skip this stage; the plan's value is Stages 1–3 plus
the boundary.

### Not planned: moving `/api/conversations*` onto MCP

Revisit only when **both** hold: an in-process server can be handed the acting principal (a fourth
`InProcessCredentialKind`), and the tier decision function from
<../../plans/information_trust_tiers.md> exists at the one console call site it is meant to live at.
Until then, moving an operator-scoped browser read onto a deliberately unscoped tool either widens
what the console shows or forces scoping into the surface that was designed not to have it.
