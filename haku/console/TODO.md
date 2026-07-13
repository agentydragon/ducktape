# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## `gmail` MCP server — Gmail API affordances not yet exposed

The in-process `gmail` server (`tools/gmail.py`) currently mirrors a slice of Gmail's REST
API (thread/message/label reads, draft creation, thread-label changes, label CRUD). Add the
rest as approval-gated tools when a workflow needs them — each maps to a Gmail API method:

- **Send / reply** — `users.messages.send`, `users.drafts.send`. High blast radius (mail
  leaves the account); keep firmly approval-gated, never a candidate for auto-approve.
- **Delete / trash** — `users.messages.{trash,untrash,delete}`,
  `users.threads.{trash,untrash,delete}`. `delete` is permanent; `trash` is recoverable.
- **Message-level label changes** — `users.messages.modify`, `users.messages.batchModify`
  (today only whole-thread label changes are exposed, via the synthesized
  `batch_modify_thread_labels`).
- **Drafts** — `users.drafts.{list,get,update,delete}` (create already exposed).
- **Attachments** — `users.messages.attachments.get` (fetch attachment bytes).
- **Raw import/insert** — `users.messages.{import,insert}`.
- **History** — `users.history.list` (incremental sync since a `historyId`).
- **Settings** — `users.settings.*`: filters (`filters.{list,get,create,delete}`),
  forwarding, vacation responder, send-as, delegates, language, IMAP/POP.
- **Watch / stop** — `users.{watch,stop}` (push notifications; needs a Pub/Sub topic).

## Agent-facing MCP server (`/mcp`) — deferred follow-ups

The `/mcp` server (`mcp_server.py`) ships with a single global auto-approval policy and a
uniform build-time tool surface. Deferred, building on a small `mcp_agents` registry table
(`agent_id` PK, `display_name`, later `policy`):

- **Connected-agent management and history filtering** — add an operator-only UI that lists every
  agent connected to that operator, then let the operator filter past tool calls by agent within
  the already-mandatory operator scope. The API/query must always constrain by both authenticated
  `operator_subject` and selected stable `agent_id`; a display name is presentation, not authority.
- **Per-agent policy** — a `policy` column authored as a structured YAML/JSON object (parsed
  into a typed Pydantic policy, same pattern as `mcp_config.py`), edited in the console UI;
  the single global policy becomes the degenerate "same for every agent" case.
- **Per-agent tool surface** — per-request `list_tools` keyed on `get_access_token().client_id`
  with `tools/list_changed` on policy edits (the list-time-identity keystone; v1's uniform
  surface avoids it).
- **Per-tool-call deep link** — a `/tool-calls/<id>` SPA route (`routing.ts` +
  `tool_calls_page.tsx`) that opens/highlights the specific call the promise `url` points at
  (today the URL loads the console but not that exact call).

## Generate result validators from `tools/list` output schemas

`export_mcp_tool_schemas.py` exports only each tool's _input_ schema. Extend it to also emit
the `outputSchema` FastMCP advertises in `tools/list` reflection and generate result
validators from it (mirroring the input-schema pipeline), so the in-process servers'
hand-written result zod schemas in
`frontend/tool_rendering/{google_calendar,gmail}/responses.tsx` (calendar event, Gmail
`Draft`) disappear. The remote `grocy-sf` result schemas stay hand-authored until its facade
exposes output schemas.
