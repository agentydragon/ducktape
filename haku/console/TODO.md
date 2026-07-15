# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

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

## Agent-facing MCP server (`/mcp`) — deferred follow-ups

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
