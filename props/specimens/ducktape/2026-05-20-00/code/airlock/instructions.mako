<%doc>
Mako template for the MCP server instructions returned during initialization.

Template variables:
  backend_instructions: dict[str, str | None]  — per-backend instructions keyed by namespace
  public_base_url: str                          — base URL for approval action links
</%doc>
# Airlock

This server **approval-wraps** calls to ${len(backend_instructions)} backend server(s).
Tools are namespaced as `{server}_{tool}` — for example, if a backend named `exec`
exposes a tool called `run_command`, it appears here as `exec_run_command`.

## How it works

When you call any tool here, the call is **queued for operator approval**. The tool
returns an `Action` JSON object — check `state.status` for the outcome.

If the action is terminal (`done` or `rejected`), you have the result immediately.
If `pending` or `executing`, the outcome will arrive via system notification (if the
OpenClaw plugin is active) or you can poll the action resource.

For `pending` actions, share the approval URL
`${public_base_url}/sessions/<session_key>/actions/<action_seq>` with the user.
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
