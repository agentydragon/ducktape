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

## Agent-facing MCP server (`/mcp`) — deferred follow-ups

The `/mcp` server (`mcp_server.py`) ships with a single global auto-approval policy and a
uniform build-time tool surface. The ordered identity, enrollment, and authorization cutover
is specified in <../../plans/oauth_architecture.md>. Do not extend the legacy DCR-client mapping
or introduce a standalone `mcp_agents` registry before that cutover establishes canonical
Operators, Agents, grants, and credential bindings.

After the cutover:

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
- **Notification-driven approval waits** — replace `_wait_terminal`'s 50 ms database polling
  with a deadline-bounded wakeup carried by PostgreSQL `LISTEN`/`NOTIFY` (building on the
  existing console event channel), followed by one final actor-scoped ledger read. This must work
  across replicas and preserve the current timeout/promise behavior.

## Generate result validators from `tools/list` output schemas

`export_mcp_tool_schemas.py` exports only each tool's _input_ schema. Extend it to also emit
the `outputSchema` FastMCP advertises in `tools/list` reflection and generate result
validators from it (mirroring the input-schema pipeline), so the in-process servers'
hand-written result zod schemas in
`frontend/tool_rendering/{google_calendar,gmail}/responses.tsx` (calendar event, Gmail
`Draft`) disappear. The remote `grocy-sf` result schemas stay hand-authored until its facade
exposes output schemas.
