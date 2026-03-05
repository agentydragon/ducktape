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

When you call any tool here, the call is **queued for operator approval**. The tool returns
a full `Action` JSON object whose `state.status` tells you what happened:

- `done` — action was approved and executed; result in `state.outcome`
- `rejected` — action was denied; reason in `state.reason`
- `pending` — still waiting for operator approval
- `executing` — approved, backend call in flight

If the action is already terminal (`done` or `rejected`), you have the result immediately.
If it is `pending` or `executing`, the outcome will be **delivered later** via system
notification (if the OpenClaw plugin is active) or you can poll the action resource.

For `pending` actions, share the approval URL
`${public_base_url}/sessions/<session_key>/actions/<action_seq>` with the user so they
can approve or reject it.

## Tool schema additions

Every wrapped tool accepts:

- `input` (required `object`): The backend tool's original arguments, nested as-is
  under this key to avoid collisions with the approval fields.
- `justification` (required `string`): Explain **why** you want to run this action.
  This is shown to the operator to help them decide. Be specific.
- `session_key` (required `string`): Your session key for result notifications.
  This is injected automatically by the OpenClaw plugin — do not set it manually.
- `wait_mode` (optional `object`): How long to wait for action resolution before
  returning. Two variants:
  - `{ "mode": "blocking" }` — wait indefinitely until terminal resolution.
  - `{ "mode": "yield_after_ms", "timeout_ms": 5000 }` — wait up to N ms then return
    current state.
  Omit to use the server default (which may be no wait).

Example call shape: `{ "input": { ...backend args... }, "justification": "...", "session_key": "...", "wait_mode": { "mode": "yield_after_ms", "timeout_ms": 5000 } }`

## Response format

Every tool call returns a full `Action` JSON object:
`{ "key": { "session_key": "...", "action_seq": 1 }, "state": { "status": "..." }, ... }`

Check `state.status` to determine the outcome:
- `done`: `state.outcome.content` holds the backend result; `state.outcome.isError`
  indicates whether the backend reported an error.
- `rejected`: `state.reason` contains the denial reason (if provided).
- `pending`: Awaiting operator decision. The outcome will arrive via notification.
- `executing`: Approved; backend call in flight. The outcome will arrive via notification.

Actions may be auto-decided by a server-side policy. With `wait_mode`, auto-decided
actions resolve within the tool call itself.

## Withdrawing an action

Call `withdraw_action(session_key, action_seq)` to cancel a pending action before an
operator decides it. Only works on actions in `pending` state.

## Checking action status

Read the action resource `resource://sessions/{session_key}/actions/{action_seq}` to
get the current state. This is useful for `pending` or `executing` actions where the
tool call returned before resolution.

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
