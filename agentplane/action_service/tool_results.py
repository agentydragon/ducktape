"""An Action's outcome as the tool it ran would have answered an MCP caller.

An MCP group's Execution stores the backend's `CallToolResult`, relayed exactly. The sandbox
executor stores its own JSON models, which appear the way FastMCP presents a model a tool returns:
the object as structured content and as one JSON text block. Any other state is said in text, as
an error result unless the Action may still produce one.
"""

from __future__ import annotations

import json

from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, TextContent

from agentplane.action_service.catalog import ExecutorBinding, McpExecutorBinding
from agentplane.action_service.models import ActionRequestView, ActionState
from agentplane.sandbox_actions.binding import SandboxExecutorBinding


def tool_result(view: ActionRequestView, executor: ExecutorBinding) -> ToolResult:
    execution, decision = view.execution, view.decision
    match view.state:
        case ActionState.SUCCEEDED:
            assert execution is not None
            match executor:
                case McpExecutorBinding():
                    return ToolResult.from_mcp_result(CallToolResult.model_validate(execution.result))
                case SandboxExecutorBinding():
                    return ToolResult(structured_content=execution.result)
        case ActionState.DECISION_PENDING | ActionState.ALLOWED | ActionState.DISPATCHING | ActionState.RUNNING:
            waiting = (
                "is waiting for the operator's decision; nothing has run yet"
                if view.state is ActionState.DECISION_PENDING
                else f"was approved and has not finished ({view.state})"
            )
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Action request {view.id} {waiting}. This is not its result: call "
                        f"get_action_result(request_id={str(view.id)!r}, wait_seconds=30) to keep waiting.",
                    )
                ],
                structured_content={"request_id": str(view.id), "state": view.state},
            )
        case ActionState.FAILED:
            assert execution is not None
            return _error(view, f"failed without a result: {json.dumps(execution.error)}")
        case ActionState.EXECUTION_UNKNOWN:
            assert execution is not None
            return _error(
                view,
                "has an unknown outcome: it may or may not have run, and it is never retried. Check its "
                f"effects before asking for it again. {json.dumps(execution.error)}",
            )
        case ActionState.DENIED:
            assert decision is not None
            reason = decision.decision_note or decision.reason_description
            return _error(view, "was denied and did not run." + (f" Reason: {reason}" if reason else ""))
        case ActionState.CANCELLED:
            return _error(view, "was cancelled before it ran.")


def _error(view: ActionRequestView, text: str) -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=f"Action request {view.id} {text}")],
        structured_content={"request_id": str(view.id), "state": view.state},
        is_error=True,
    )
