"""Props-specific mock utilities."""

from collections.abc import Generator

from agent_core.testing.mcp.responses import MCPDecoratorMock
from agent_core.testing.responses import tool_roundtrip
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs
from openai_utils.model import FunctionCallItem, ResponsesRequest, SystemMessage


def get_system_message_text(req: ResponsesRequest) -> str:
    """Extract full system message text from a ResponsesRequest.

    Concatenates all text parts from all SystemMessage items in the request.
    Useful for mocks that need to verify the system prompt contains expected content.
    """
    if isinstance(req.input, str):
        return ""

    return "\n".join(part.text for item in req.input if isinstance(item, SystemMessage) for part in item.content)


class SubprocessExecMock(MCPDecoratorMock):
    """Mock for in-container subprocess exec (DirectToolProvider).

    Uses plain tool name ``exec`` matching DirectToolProvider registration
    in in-container agent loops (critic, grader, critic-dev).

    For host-side docker exec via MCP server (editor_agent), use
    DockerExecMock from agent_core.testing.mcp.responses instead.
    """

    def exec_roundtrip(
        self, cmd: list[str], *, timeout_ms: int = 5000, cwd: str | None = None, max_bytes: int = 100_000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield exec call for in-container subprocess, return typed result."""
        exec_args = DirectExecArgs(cmd=cmd, timeout_ms=timeout_ms, cwd=cwd, max_bytes=max_bytes)
        call = self.tool_call("exec", exec_args)
        return tool_roundtrip(call, BaseExecResult)
