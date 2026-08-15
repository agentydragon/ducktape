# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## The console as a channel, not a viewer

Direction set 2026-08-15: Matrix and the console frontend should be two **messaging channels**
onto one session, each able to do broadly what the other can. Today the console is split across
two pages that are the same object, cannot show a session's lifecycle at all, and cannot speak.
Design, the parity gaps it closes, and the traps in each: <plans/session_channels.md>.

1. **Render bootstrap narration** — `setup_output` rows already exist in the frame log; nothing
   but the console renders them. Smallest thing that makes it a channel.
2. **Live updates over the existing `/api/events/ws`**, as a `SessionChangedEvent {session_id}`
   the page refetches on — not a second SSE stream, and not a poll. **Coalesce per session**:
   `SessionEventKind.UPDATE` fires per stream delta, and a full-transcript refetch per delta per open
   tab is the O(session) cost <../plans/chat_runtime_cleanup.md> § Anytime flags on the SSE path.
3. **Merge `/chat` into `/conversations`** as one sessions surface — list plus detail, with new /
   compose / abort / close as actions on a session. Costs the per-token SSE stream; take it
   knowingly.
4. **Record lifecycle transitions** as frame-log rows, the way narration already is, so a session
   that never got past `provisioning` has a durable record. **Not** the status line or the typing
   indicator — those are renderings of live state each channel derives for itself.
5. **Reconcile a channel against the session** rather than sending to it: a loop per
   `(channel, session)` over a cursor on cleanup stage 7's `chat_attachment`. A channel that holds
   its own copy (Matrix) needs it; one that reads the record (the console) converges by refetching.
6. **Send into a Matrix session** (lower priority) — the console holds only `@haku`'s credential,
   so an operator message reaches the room as a **relay** posted by Haku's account and tagged with
   its true provenance. Under the loop the send only enqueues; the room being one message behind
   is a divergence the reconciler already closes. The subtle part is `_is_conversational`, which
   must count a relay as conversation or every rotation re-awakens a session with the operator's
   half missing.

Two more, outside that spine. **Slash commands** give Matrix the non-message actions the console
has — abort first — as ingress interception rather than an agent tool, so R5.4 is untouched;
watch out for Element consuming leading-slash verbs before they ever reach the room. And
**interlink the two channels** now that sessions have a page: a link to the console session in
the R7.2 notice, a `matrix.to` permalink back, session ↔ tool calls. A posted Matrix event is
permanent and federated, so settle the session route (item 3) before minting links into a room.

## Notification text per tool kind

A push notification is titled with the tool's shared action description
(`frontend/tool_rendering/<server>/actions.ts`) — the same one-line summary the approvals card's
identity line shows. That is the right default, but a notification is a different surface: no
arguments visible, no expand affordance, read on a lock screen, and it is the one place a call
can be approved without seeing its arguments at all. Some tools would be better served by
notification-specific wording — naming the actual target ("Delete Pod haku-console-7f9 in
haku-console") where the card can rely on the widget below it to show that.

Add an optional per-tool notification override alongside the action description, falling back to
it when absent. Deliberately not done in the change that introduced push: the shared description
is the honest starting point, and which tools actually warrant divergence is worth learning from
real notifications rather than guessing up front.

## `gmail` MCP server — Gmail API affordances not yet exposed

The in-process `gmail` server (`tools/gmail.py`) currently mirrors a slice of Gmail's REST
API (thread/message/label/filter reads, draft CRUD, thread-label changes, label CRUD, filter
create/delete). Add the rest as approval-gated tools when a workflow needs them — each maps
to a Gmail API method:

- **Send / reply** — `users.messages.send`, `users.drafts.send`. High blast radius (mail
  leaves the account); keep firmly approval-gated, never a candidate for auto-approve.
- **Delete / trash** — `users.messages.{trash,untrash,delete}`,
  `users.threads.{trash,untrash,delete}`. `delete` is permanent; `trash` is recoverable.
- **Message-level label changes** — `users.messages.modify`, `users.messages.batchModify`
  (today only whole-thread label changes are exposed, via the synthesized
  `batch_modify_thread_labels`).
- **Attachments** — `users.messages.attachments.get` (fetch attachment bytes).
- **Raw import/insert** — `users.messages.{import,insert}`.
- **History** — `users.history.list` (incremental sync since a `historyId`).
- **Settings** — `users.settings.*`: forwarding, vacation responder, send-as, delegates,
  language, IMAP/POP (filters are already exposed).
- **Watch / stop** — `users.{watch,stop}` (push notifications; needs a Pub/Sub topic).

**Draft message shape is flat.** `drafts_create`/`drafts_update` take plain-text
`to`/`cc`/`bcc`/`subject`/`body` and build the MIME server-side, so they leave a lot on the
table — attachments, an HTML alternative part, arbitrary headers, or a raw RFC 2822 message.
If a workflow needs more than the flat fields, accept a richer message representation from the
client (e.g. an `html_body`, an attachments list, or a raw passthrough) rather than growing the
flat parameter list one field at a time.

## `google_calendar` MCP server — Calendar API affordances not yet exposed

Audited against the Google Calendar API v3 reference on 2026-07-14:
<https://developers.google.com/workspace/calendar/api/v3/reference>. The current server exposes
`create_event`, `get_event`, `list_events`, and `list_event_instances`; authenticated-agent reads
auto-approve, while creation stays operator-approved. The remaining public API is intentionally
deferred:

- **Event recurrence and mutation** — accept Google-supported `RDATE`, `EXDATE`, and `EXRULE`
  content lines; update or delete a whole series or one instance; and implement "this and
  following" as the documented trim-old-series + insert-new-series operation. Before exposing
  these, specify exception preservation, optimistic concurrency, attendee notifications, and
  partial-failure recovery.
- **Remaining Events methods** — `events.delete`, `import`, `move`, `patch`/`update`, `quickAdd`,
  and `watch`. Deletes/moves/updates need explicit approval scope and etag behavior; import and
  quick-add need clear reasons to coexist with typed creation; watch needs durable callback and
  renewal infrastructure.
- **Remaining Events list/sync controls** — incremental `syncToken`/`nextSyncToken`, `updatedMin`,
  `showDeleted`, `showHiddenInvitations`, `iCalUID`, `eventTypes`, private/shared extended-property
  filters, `maxAttendees`, `orderBy`, and response `timeZone`. Add these as real workflows emerge,
  preserving Google's incompatible-parameter rules in the MCP schema.
- **Remaining Event fields** — attachments, Meet `conferenceData`, attendee `sendUpdates`, custom
  event ids, colors/event labels, visibility/transparency, guest permissions, source, extended
  properties, reminders using calendar defaults, and specialized birthday/focus-time/
  out-of-office/working-location event types. Each addition needs typed arguments, an approval
  preview, and tests against that event type's Google restrictions.
- **Calendar discovery and availability** — `calendarList.get/list/insert/patch/update/delete/watch`,
  `calendars.get`, `colors.get`, `freebusy.query`, and `settings.get/list/watch`. Read-only discovery,
  colors, free/busy, and settings may be candidates for standing read approval; calendar-list
  mutations remain manual. Watch methods share the push-infrastructure prerequisite below.
- **Calendar administration and sharing** — `calendars.insert/patch/update/delete/clear`,
  and every `acl.get/list/insert/patch/update/delete/watch` method. These need separate
  administrative intent, destructive confirmation, and any additional Google or Workspace-admin
  scopes; `clear` and ACL writes must never auto-approve.
- **Push channels** — resource watches plus `channels.stop`. Do not expose until haku-console owns
  authenticated webhook delivery, durable channel metadata, expiration renewal, deduplication,
  replay/catch-up, and cleanup on disconnect.

## MCP server (`/mcp`) — deferred follow-ups

The `/mcp` server (`mcp_server.py`) now resolves canonical Operators, Agents, grants, and
credential bindings through one authority, while retaining a single global auto-approval policy
and deriving each request's tool surface from that Agent's Operator connections. The architecture
is specified in <../../plans/oauth_architecture.md>. The next product slices are:

- **Connected Agents** — add an Operator-scoped API and UI showing each Agent's name, client
  software, scopes, status, creation and last-seen times, and reconnect history.
- **Agent-filtered history** — filter past tool calls by Agent only after applying the
  authenticated Operator predicate. Resolve display names through canonical joins; never copy
  them into tool-call rows or use them as authority.
- **Agent lifecycle controls** — expose revoke/disable, rename/history, and tombstone/reconnect
  operations as vertical API + UI + audit-event slices.
- **Per-Agent policy** — author a typed structured policy in the console UI. The single global
  policy remains the degenerate "same for every Agent" case until this ships.
- **Per-Agent tool surface** — derive request-time `list_tools` from the verified binding and
  policy, with `tools/list_changed` on policy edits. Do not key authorization directly on an
  unverified DCR `client_id`.
- **Per-tool-call deep link** — a `/tool-calls/<id>` SPA route (`routing.ts` +
  `tool_calls_page.tsx`) that opens/highlights the specific call the promise `url` points at
  (today the URL loads the console but not that exact call).

## Serve a last-known tool catalog for a degraded server

A degraded server reports no tools at all, so an agent can see that `home-assistant` exists, see
exactly why it is unreachable, and still not learn a single tool name — even though the console
reflected that catalog successfully minutes earlier. Connection state and catalog knowledge are
orthogonal: a tool list is _what this server has_, not _may this caller reach it right now_.
Operator decisions already taken (2026-08-10):

- **Status reads only.** `get_mcp_server_status` may serve a stale catalog, explicitly marked with
  when it was reflected. `tools/list` must keep contributing nothing for a degraded server —
  discovery deliberately fails closed once an Operator disconnects, and handing back
  callable-looking proxy tools would reverse that. Knowing a name is not authorization: execution
  re-resolves credentials and still fails.
- **Persisted in Postgres**, not in the reflection cache. Two reasons, both load-bearing:
  - The cache key is `(server_id, config_fingerprint, credential_fingerprint)` and that third
    component _is_ the fail-closed property (see `mcp_reflection_cache`'s module docstring). A
    last-known lookup cannot use it, so this needs its own key — scope it per
    `(operator_id, server_id, config_fingerprint)` so one Operator's tool list never surfaces for
    another, since upstreams may vary tools by account.
  - The cache is per-replica, in-memory, and `_prune` drops entries at expiry (60s default), so
    there is no long-term memory to serve and a rollout would empty it anyway. The outage that
    motivated this ran three days.

Two traps for whoever picks this up:

- **The failure that motivated this never reaches the cache.** `home-assistant` was
  `failure_stage: credential_resolution`, and `metadata_for_operator` returns `DegradedReflection`
  before it ever calls the dispatcher. Only `tool_discovery` failures get that far, so the
  last-known lookup belongs in `get_mcp_server_status`, above the dispatcher — not inside
  `McpServerDispatcher.metadata`.
- **`_exposed_metadata` early-returns on `DegradedServerState`.** Stale tools must go through the
  same projection, or a caller gets raw upstream schemas with no `approval_mode` and sends the
  wrong payload shape to `call_mcp_tool` — the exact failure the exposed reflection exists to
  prevent.

## Operator browser auth — parked remainders

From the login audit (<debug/2026_07_24_operator_login_audit.md>), fixed in #3516/#3519 except for:

- **A background 401 still navigates the tab** (audit F3). Expiry is now announced beforehand and
  re-authentication returns to the same page, but the redirect itself is still fired by whichever
  poll happens to fail first, and the top-level navigation discards whatever is unsaved in the
  framed haku-ui. The alternative is an explicit "session expired — sign in" state the operator
  clicks, so the frame survives until they choose. Superseded entirely if session renewal lands
  (<plans/operator_session_renewal.md>).
- **No sign-out affordance** (audit F6). `/auth/logout` exists and is exact-Origin gated, but
  nothing in the SPA calls it, and it clears only the console session — not Authentik's — so a
  manual logout silently re-logs-in on the next 401. Needs RP-initiated logout to be meaningful.

## The chat runtime's timings are module constants, not configuration

`ClaudeRuntimeConfig` carries the deploy wiring (namespace, warm pool, proxy, MCP URL) and exactly
one timing — `session_ttl_seconds`. Every other number the runtime's behaviour depends on is a
module-level constant, so changing one is a code edit, a CI build and a roll. The ones that are
genuinely operational knobs should move onto the config model:

- `x/room_status.py` — `STATUS_AFTER_SECONDS` (8s before a turn says anything, R6.2),
  `STATUS_EDIT_INTERVAL_SECONDS` (5s edit floor, R6.5), `TYPING_REFRESH_SECONDS`.
- `x/claude_chat.py` — `LEASE_TTL` / `LEASE_RENEW_INTERVAL`, `PROVISION_LEASE`, `ADOPTION_GRACE`.
- `x/matrix_pacer.py` — `SENDS_PER_SECOND`, `SEND_BURST`, `MAX_QUEUED_SENDS`, `FLUSH_SECONDS`.
- `x/matrix_session.py` — `SUPERVISE_INTERVAL`, `LEADER_RETRY`, `PROVISION_BACKOFF`,
  `RE_AWAKENING_MESSAGES` (the N of R3.3a).
- `x/matrix_sync.py` — `ERROR_BACKOFF`, `REFUSED_BATCH_BACKOFF`, and `MAX_BACKFILL_PAGES` /
  `TIMELINE_LIMIT` from `x/matrix_client.py`.
- `runtime/x/claude_bridge/runner.py` — `MAX_DISCONNECTED_SECONDS`, `REPLAY_WINDOW`,
  `RECONNECT_{BASE,MAX}_DELAY`. **These live in the runner**, whose image is pinned at claim
  creation, so they are not console config at all: they reach a running sandbox only through the
  launch, or not until it is replaced.

**Not everything here is a knob, and the split is the point.** `TYPING_TIMEOUT_MS` and
`SYNC_TIMEOUT_MS` are the homeserver's own semantics, `MAX_RATE_LIMIT_RETRIES` exists to bound a
nio behaviour (<docs/chat_runtime_facts.md>), and the `*_FRAME_KIND` strings are wire vocabulary.
Making those configurable would invite a deploy that contradicts a protocol. Move the timings;
leave the facts where the code that depends on them can be read beside them.

Two things worth settling in the same change, since they are the same question: the three values
<../plans/matrix_chat_runtime.md> § Open questions never chose — **batch cap** (R2.6), **debounce
window** (R2.7) and **age fence** (R2.8) — should arrive as config with a default rather than as
another constant, because the whole reason they are unchosen is that the right value is an
operational finding. And a value read per use rather than at startup is what makes tuning a
ConfigMap edit instead of a roll.

## Finish the `claude_chat` → `session` rename

The tables, the wake channel, the Python and the operator routes moved; three things are
deliberately still holding the old name, each for its own reason.

- **The legacy `/api/claude/sessions/…` registrations**, which exist only so a browser tab loaded
  before the roll keeps working until it reloads. Drop them one release after they ship, when no
  deployed bundle names them.
- **`/internal/claude/runner/{session_id}`.** Left alone on purpose: the runner image dials it, so
  renaming it is a coordinated two-sided roll, not part of a console-only change.
- **`x/claude_chat.py` itself**, and the SPA's `frontend/x/claude_chat_page.tsx`. The module split is
  its own item (<../plans/chat_runtime_cleanup.md> § Anytime), and renaming the file now would
  collide with it.

## `session_frames` does not map to one concept

`kind` holds two discriminator vocabularies at once. `RolloutRecorder` — a `FrameSink` on
`ClaudeCli`, so it structurally only ever sees CLI protocol frames — writes the CLI's own top-level
`type` there. `_progress_reporter` writes `setup_output`, which is the **bridge envelope's** `kind`
literal, for one decoded line of a `SetupOutput`. Two unrelated sinks, one column, two vocabularies.

A `partial` row is a third thing again: the console's own reconstruction of an answer still
streaming, which wears `assistant` and is told apart by a boolean column rather than by `kind`.

**Consequences, so this reads as a known state rather than an oversight:**

- There is deliberately **no enum over `kind`**. One would give a name to a concept the schema does
  not have, and an enum over the union of two vocabularies is what made the first attempt confusing
  enough to back out.
- The loose `*_FRAME_KIND` constants in `x/session_frames.py` stay loose, with a pointer to
  `SessionFrame` and to the projection plan's stage 2.
- The table's own docstring says the same thing, since that is where a reader meets it first.

The `partial` row leaves on its own: it is tombstoned on `update_partial_frame`, and recording the
stream deltas removed its reason to exist. What is left after that is the two-vocabulary problem.

`../plans/chat_runtime_projection.md` § stage 2 holds the intended shape — the table becomes the log
of the bridge, `kind` becomes the envelope discriminator, and the CLI's type gets its own column —
along with what that costs (the sink has to move down to `WebSocketTransport`, and it is three
releases because flipping a column's meaning under a rolling deploy is not additive). **Nothing is
scheduled**, and no other work depends on it.

## Scope conversation reads to the reader's trust tier

**The policy is decided** (operator, 2026-08-15) and now needs building. `list_conversations`,
`read_rollout`, and `list_turns` (<tools/conversations.py>) are unscoped — any Haku may read any
session, whichever room or operator it served — under R5.3a's deliberate deferral, whose stated
condition for revisiting (more than one agent) has arrived. An agent reads the transcripts and
conversations **its tier** gives it. The fence is the tier, **not** the room: cross-room and
cross-session reads stay open within a tier, so an agent keeps its own history.

Three things it needs, in order:

1. **A tier on `sessions`**, beside the `surface`/`room_id` that landed in `0030` —
   from the room's fixed tier for a Matrix session, from the agent kind otherwise, with the room's
   authoritative where both exist.
2. **Unlabelled reads as highest**, so every session predating the column is unreadable by a lower
   tier rather than treated as unclassified-therefore-fine.
3. **The decision function at the one call site** R5.3a identified, in the shape the approval
   policy already has — never scoping smeared through the transport.

**Semantic search is the urgent half, because it is already unscoped and live.** `haku_index`'s
`search`/`index_status` were exposed to Haku unscoped on 2026-08-15 — a fair call then, since they
granted no reachability `haku_conversations` did not already have — and
<../state_index/README.md> § Read scoping names the condition for revisiting: the moment a room
Haku should not see exists, ranked retrieval is where it leaks first. Several agents is that
moment. A drilldown makes reading another conversation deliberate; ranked retrieval surfaces it by
accident, at the top of the results.

**Do it by naming indexes rather than filtering rows.** `Corpus` (<../state_index/schema.py>)
stays the **type** — `git`/`chat`, deciding how content is chunked and addressed — and gains named
**instances** configured per repo and per tier ("index `foobar` is of type `git`, indexes this
remote"), in the discriminated shape `mcp.servers` already uses. The gate becomes "which indexes
may this agent search", checked once in <state_index_reader.py>, which shrinks in the process:
its hardcoded `haku_state`/`conversations` → `git`/`chat` mapping is replaced by config names, so
what is left is the permission check. `haku_recall_reads` becomes per-index grants. That beats a
per-row tier filter — a missed predicate on one read path leaks, an index an agent cannot name
does not. `chunks` is untouched: it is a content-addressed embedding cache, identity lives on the
occurrence rows, and the instance goes there. Full design:
<../plans/information_trust_tiers.md>. The wider inventory — the drilldown tools, the identity it
keys on, and the RLS-scoped-Postgres-role alternative — stays in <../state_index/README.md>
§ Read scoping.
