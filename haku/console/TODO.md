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

## Auto-approval policy (deferred)

Design + implement the policy that lets defined `gmail` calls skip the operator click — e.g.
label operations scoped to the `haku/` prefix — so haku-console's approval queue can
auto-allow the safe, structurally-bounded subset while everything else stays human-gated.
Scope it in `mcp_approval.py` (short-circuit `submit` to `RUNNING` for allowlisted calls).

## Generate result validators from `tools/list` output schemas

`export_mcp_tool_schemas.py` exports only each tool's _input_ schema. Extend it to also emit
the `outputSchema` FastMCP advertises in `tools/list` reflection and generate result
validators from it (mirroring the input-schema pipeline), so the in-process servers'
hand-written result zod schemas in
`frontend/tool_rendering/{google_calendar,gmail}/responses.tsx` (calendar event, Gmail
`Draft`) disappear. The remote `grocy-sf` result schemas stay hand-authored until its facade
exposes output schemas.
