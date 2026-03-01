<%doc>
Mako template for the MCP server instructions returned during initialization.

Template variables:
  backend_instructions: dict[str, str | None]  — per-backend instructions keyed by namespace
  public_base_url: str                          — base URL for approval action links
</%doc>
# Approval Gate

This server **approval-wraps** calls to ${len(backend_instructions)} backend server(s).
Tools are namespaced as `{server}_{tool}` — for example, if a backend named `exec`
exposes a tool called `run_command`, it appears here as `exec_run_command`.

## How it works

When you call any tool here, the call is **queued for operator approval** and returns
immediately with an `ActionKey` (`session_key` + `action_seq`). Your call is **not executed yet**.

Share the approval URL `${public_base_url}/sessions/<session_key>/actions/<action_seq>`
with the user so they can approve or reject the action.

Once the operator approves it, the call is forwarded to the correct backend server and the
result is recorded. If the OpenClaw plugin is active, the outcome will be **injected
into your session** automatically; otherwise, poll or subscribe to the session log HWM.

## Tool schema additions

Every wrapped tool accepts:

- `input` (required `object`): The backend tool's original arguments, nested as-is
  under this key to avoid collisions with the approval fields.
- `justification` (required `string`): Explain **why** you want to run this action.
  This is shown to the operator to help them decide. Be specific.
- `session_key` (required `string`): Your session key for result notifications.
  This is injected automatically by the OpenClaw plugin — do not set it manually.

Example call shape: `{ "input": { ...backend args... }, "justification": "...", "session_key": "..." }`

## Response format

Every tool call returns immediately with an `ActionKey` JSON object:
`{ "session_key": "...", "action_seq": 1 }`

Actions may be auto-decided by a server-side policy. If auto-denied, the action will
appear as `rejected` when you read its resource — there is no separate error path.

## Withdrawing an action

Call `withdraw_action(session_key, action_seq)` to cancel a pending action before an
operator decides it. Only works on actions in `pending` state.

## Checking action status

Read the action resource `resource://sessions/{session_key}/actions/{action_seq}` to
get the current state.

The resource returns the full `Action` JSON including `state.status` which is one of:
`pending`, `executing`, `done`, `rejected`, `withdrawn`.

For `done` actions, `state.outcome.content` holds the backend result and
`state.outcome.isError` indicates whether the backend reported an error.

## Session event log

Each session has an append-only event log. Subscribe to the log high-water mark (HWM)
resource for a session to be notified of any state changes:

- `resource://sessions/{session_key}/log_hwm` — returns the entry_id of the last log entry
- `resource://sessions/{session_key}/log/{entry_id}` — returns a specific log entry

Log event kinds: `action_received`, `approved`, `denied`, `withdrawn`,
`execution_started`, `execution_finished`.
% if backend_instructions:

---

## Backend servers
% for namespace, instructions in sorted(backend_instructions.items()):

### `${namespace}`
% if instructions:
${instructions}
% else:
No additional instructions provided.
% endif
% endfor
% endif
