"""Props-specific mock utilities."""

from collections.abc import Generator

from agent_core.testing.mcp.responses import MCPDecoratorMock
from agent_core.testing.responses import tool_roundtrip
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs
from openai_utils.model import FunctionCallItem, ResponsesRequest


def get_system_prompt_text(req: ResponsesRequest) -> str:
    """The system prompt a ResponsesRequest carries, for a mock asserting on it.

    The Responses API's own top-level `instructions` field, which is where the agent
    framework's OpenAI Responses client puts an agent's `instructions`. It used to prepend
    them to `input` as a system message instead, so a reader scanning `input` saw the prompt
    until agent-framework-openai 1.14.3 and an empty string after it.
    """
    return req.instructions or ""


class SubprocessExecMock(MCPDecoratorMock):
    """Mock for in-container subprocess exec (DirectToolProvider).

    Uses plain tool name ``exec`` matching DirectToolProvider registration
    in in-container agent loops (critic, grader, critic-dev).
    """

    def exec_roundtrip(
        self, cmd: list[str], *, timeout_ms: int = 5000, cwd: str | None = None, max_bytes: int = 100_000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield exec call for in-container subprocess, return typed result."""
        exec_args = DirectExecArgs(cmd=cmd, timeout_ms=timeout_ms, cwd=cwd, max_bytes=max_bytes)
        call = self.tool_call("exec", exec_args)
        return tool_roundtrip(call, BaseExecResult)
