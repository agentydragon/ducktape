# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

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

## Approvals drawer

- **A withdrawn call vanishes from the drawer with no explanation.** An agent withdrawal removes
  the card from the queue mid-review; the operator sees a silent drop. Fixing it properly means
  re-fetching disappeared ids in `applyToolApprovals` (`haku_ui_embed.tsx`) and surfacing the
  withdrawal the way a decided call surfaces in "Recent".

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

## `request_close` wakes nobody

`request_close` (`x/claude_chat.py`) sets `status = "closing"` and notifies nothing — it is the
only status mutation on that path that does not. A closing session's runner therefore stays in
`wait(ChatEventKind.PROMPT, …)` until its own 30-second timeout before it notices, which is a real
30-second lag in session teardown.

Since the channel merge (#3938, #3941) the event kind is an argument rather than a channel name, so
the fix is one `await notify(db, ChatEventKind.PROMPT, session_id)` alongside the status write. It
must stay **inside** that transaction: `pg_notify` delivers on commit.

## No `startupProbe` on the console containers

Both containers in <../../cluster/k8s/haku/console/deployment.yaml> carry a `livenessProbe`
(`initialDelaySeconds: 10`, `periodSeconds: 30`) and no `startupProbe`. The API container applies the
Alembic baseline at startup before it serves, so slow startup work competes with the liveness budget
and a start that overruns it is killed and retried — worst exactly when a migration has the most to
do.

A `startupProbe` on the same endpoint with a generous `failureThreshold` separates "still starting"
from "wedged" and lets the liveness budget stay tight for steady state. Deliberately not solved by
loosening `livenessProbe`, which would blunt detection for the whole life of the pod to buy slack
that is only needed once.

## `claude_chat_frames` does not map to one concept

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
- The loose `*_FRAME_KIND` constants in `x/claude_chat.py` stay loose, with a pointer here.
- The table's own docstring says the same thing, since that is where a reader meets it first.

The `partial` row leaves on its own: it is tombstoned on `update_partial_frame`, and recording the
stream deltas removed its reason to exist. What is left after that is the two-vocabulary problem.

`../plans/chat_runtime_projection.md` § stage 2 holds the intended shape — the table becomes the log
of the bridge, `kind` becomes the envelope discriminator, and the CLI's type gets its own column —
along with what that costs (the sink has to move down to `WebSocketTransport`, and it is three
releases because flipping a column's meaning under a rolling deploy is not additive). **Nothing is
scheduled**, and no other work depends on it.
## Which past conversations may an agent read?

`list_conversations`, `read_rollout`, and `list_turns` (<tools/conversations.py>) are unscoped by
deliberate deferral — R5.3a in <../plans/matrix_chat_runtime.md> left the policy open rather than
guess at a rule nobody stated. Any Haku may read any session, whichever room or operator it served.

Semantic search over the same corpus (<../state_index/README.md>, `chat`) raises the stakes without
changing the data: a drilldown makes reading another room's conversation a deliberate act, where
ranked retrieval surfaces it by accident at the top of the results. That index is not exposed to any
agent yet, and settling this is the prerequisite for exposing it.

The full inventory of what a scope would touch — the search query, the drilldown tools it hands off
to, the identity it keys on, the auto-approval config, and the RLS-scoped-Postgres-role alternative —
is in <../state_index/README.md> § Read scoping. Record the decision in R5.3a once made, and in
<../docs/security.md> if the answer turns out to be "any conversation".
