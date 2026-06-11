"""Bootstrap handler for init script execution.

This module contains BootstrapHandler which is an application-layer component
that monitors init script execution and aborts on failure.
"""

from __future__ import annotations

from mcp.types import CallToolResult, TextContent

from agent_core.events import ToolCallOutput
from agent_core.handler import BaseHandler
from agent_core.loop_control import InjectItems, LoopDecision, NoAction
from agent_pkg.host.init_runner import InitFailedError
from mcp_infra.exec.models import BaseExecResult, Exited, TruncatedStream
from openai_utils.model import FunctionCallItem

__all__ = ["BootstrapHandler"]


def _extract_text(stream: str | TruncatedStream) -> str:
    return stream.truncated_text if isinstance(stream, TruncatedStream) else stream


def _raise_if_failed(exec_result: BaseExecResult) -> None:
    if not (isinstance(exec_result.exit, Exited) and exec_result.exit.exit_code == 0):
        stderr_text = _extract_text(exec_result.stderr)
        stderr_preview = stderr_text[:500] if stderr_text else ""
        raise InitFailedError(
            f"Init script failed: {exec_result.exit.model_dump()}\nstderr: {stderr_preview}", exec_result=exec_result
        )
    if isinstance(exec_result.stdout, TruncatedStream):
        raise InitFailedError(
            f"Init script output truncated: {exec_result.stdout.model_dump()}", exec_result=exec_result
        )
    if isinstance(exec_result.stderr, TruncatedStream):
        raise InitFailedError(
            f"Init script output truncated: {exec_result.stderr.model_dump()}", exec_result=exec_result
        )


class BootstrapHandler(BaseHandler):
    """Execute init script and abort on failure.

    Injects an init call as the first action and monitors its result. If the
    init script fails (non-zero exit code / isError=True / truncated output),
    raises InitFailedError to abort the agent run immediately.

    This enables init scripts to perform environmental sanity checks before
    the agent begins execution:
    - Verify database credentials are valid
    - Check that the MCP server is reachable
    - Validate expected resources exist (snapshots, ground truth data, etc.)
    - Ensure required tools are available in the container

    Truncation detection: If the docker_exec output was truncated (stdout or
    stderr contains a TruncatedStream object with truncated_text/total_bytes),
    the bootstrap is considered failed since important context may be missing.
    """

    def __init__(self, init_call: FunctionCallItem):
        if not isinstance(init_call, FunctionCallItem):
            raise TypeError(f"init_call must be FunctionCallItem, got {type(init_call).__name__}")
        self._init_call = init_call
        self._init_complete = False

    def on_tool_result_event(self, evt: ToolCallOutput) -> None:
        if evt.call_id != self._init_call.call_id:
            return

        self._init_complete = True

        result: CallToolResult = evt.result

        if result.isError:
            if result.structuredContent:
                error_content = result.structuredContent
                error_text = error_content.get("error") if isinstance(error_content, dict) else str(error_content)
                raise InitFailedError(f"Init script failed: {error_text or 'error flagged'}")
            for block in result.content:
                if isinstance(block, TextContent):
                    raise InitFailedError(f"Init script failed: {block.text}")
            raise InitFailedError("Init script failed (error flagged)")

        if not result.structuredContent:
            raise InitFailedError("Init script returned no structured content")

        exec_result = BaseExecResult.model_validate(result.structuredContent)
        _raise_if_failed(exec_result)

    def on_before_sample(self) -> LoopDecision:
        if not self._init_complete:
            return InjectItems(items=[self._init_call])
        return NoAction()

    @property
    def init_complete(self) -> bool:
        return self._init_complete
