"""Canonical guidance for agents using Haku Console's MCP surface.

Second copy on purpose: the chat prompts carry the same guidance as template text
(cluster/k8s/haku/console/chat_prompt_fragment.md.j2 § Haku Console MCP), because some MCP
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

{_DETAILS_POINTER}"""


def approval_request_preamble(*, tool: str, server: str) -> str:
    """Return concise per-tool guidance for clients that hide server instructions."""
    return (
        f"`{tool}` on `{server}` requires operator approval. Put real arguments under `input`, include `rationale`, "
        "and use the `wait_for_result_ms` bound; a timeout returns a non-terminal stub. Poll "
        "`get_tool_call(tool_call_id)`; withdraw only your own pending call."
    )
