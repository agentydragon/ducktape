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
immediately with an `action_id`. Your call is **not executed yet**.

Share the approval URL `${public_base_url}/actions/<action_id>` with the user so they
can approve or reject the action.

Once the operator approves it, the call is forwarded to the correct backend server and the
result is recorded. If the OpenClaw plugin is active, the outcome will be **injected
into your session** automatically; otherwise, poll or subscribe to the action resource.

## Tool schema additions

Every wrapped tool accepts:

- `input` (required `object`): The backend tool's original arguments, nested as-is
  under this key to avoid collisions with the approval fields.
- `justification` (required `string`): Explain **why** you want to run this action.
  This is shown to the operator to help them decide. Be specific.
- `session_key` (optional `string`): Your session key for result notifications.
  This is injected automatically by the OpenClaw plugin — do not set it manually.

Example call shape: `{ "input": { ...backend args... }, "justification": "..." }`

## Response format

Every tool call returns immediately with the `action_id` UUID string.

Actions may be auto-decided by a server-side policy. If auto-denied, the action will
appear as `rejected` when you read its resource — there is no separate error path.

## Withdrawing an action

Call `withdraw_action(action_id)` to cancel a pending action before an operator
decides it. Only works on actions in `pending` state.

## Checking action status

Read the action resource `resource://actions/{action_id}` to get the current state.

The resource returns the full `Action` JSON including `state.status` which is one of:
`pending`, `executing`, `done`, `rejected`, `withdrawn`.

For `done` actions, `state.outcome.content` holds the backend result and
`state.outcome.isError` indicates whether the backend reported an error.

## Subscribing to updates

Subscribe to `resource://actions/{action_id}` to receive `ResourceUpdated`
notifications whenever the action state changes. Unsubscribe once the action
reaches a terminal state (`done`, `rejected`, or `withdrawn`).
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
