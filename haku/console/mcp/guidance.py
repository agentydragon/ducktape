"""Canonical guidance for agents using Haku Console's MCP surface.

Second copy on purpose: the chat prompts carry the same guidance as template text
(cluster/k8s/haku/console/conversation_prompt_fragment.md.j2 § Haku Console MCP), because some MCP
clients never show server instructions to the model. Edit both together.
"""

from __future__ import annotations

_DETAILS_POINTER = "See https://github.com/agentydragon/ducktape for details."


SERVER_INSTRUCTIONS = f"""Haku Console MCP tools are an operator-owned proxy.

- Tools are named `<server>__<tool>`. Use each tool's advertised input schema as the source of truth.
- Approval-required tools take an `input` object containing the real arguments and a `rationale`; they
  return the result when approved and completed within the `wait_for_result_ms` bound, or a non-terminal
  stub. A `pending_approval` stub remains queued; `running` means it was approved but is still
  executing. Some tools may be auto-approved under the reviewed policy.
- Poll `get_tool_call(tool_call_id)` for a stub. Withdraw only your own calls while they are still
  `pending_approval`; withdrawal never stops an approved call.
- If a tool is missing from your tool list, use
  `get_mcp_server_status(server_id, include_tool_schemas=True)` to learn its exposed schema, then
  `call_mcp_tool(server_id, tool_name, arguments)` with that exact shape. `list_mcp_servers` passively
  reports configured connection state.

Access is scoped, and you can request more:

- HTTP(S) egress goes through a fence; only origins a standing policy or an active grant covers
  will connect, and an https denial is a nearly mute `CONNECT tunnel failed, response 403` — read
  it as "no grant covers this origin", not an outage. Kubernetes access is gated the same way;
  check `grants__kubernetes_can_i` before assuming a capability.
- When a task needs access you lack, request it with `grants__create_grant`: exact origins
  (scheme + host + explicit port; a redirect to another host needs its own entry) or exact
  namespaces + verbs, the narrowest coverage that serves the task, a short `duration_seconds`,
  and a `rationale` written for the operator deciding it. Batch one task's needs into one call.
  Prefer the `session` principal for one-task needs; `revoke_grants` what you no longer need.

{_DETAILS_POINTER}"""


def approval_request_preamble(*, tool: str, server: str) -> str:
    """Return concise per-tool guidance for clients that hide server instructions."""
    return (
        f"`{tool}` on `{server}` requires operator approval. Put real arguments under `input`, include `rationale`, "
        "and use the `wait_for_result_ms` bound; a timeout returns a non-terminal stub. Poll "
        "`get_tool_call(tool_call_id)`; withdraw only your own pending call."
    )
