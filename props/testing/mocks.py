"""Props-specific mock utilities."""

from collections.abc import Generator
from uuid import UUID

from more_itertools import one
from pydantic import TypeAdapter

from agent_core.testing.mcp.responses import MCPDecoratorMock
from agent_core.testing.responses import DecoratorMock, tool_roundtrip
from mcp_infra.exec.models import BaseExecResult, make_exec_input
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest, SystemMessage
from props.grader.tools import FillRemainingArgs, ListPendingArgs, PendingEdge


def get_system_message_text(req: ResponsesRequest) -> str:
    """Extract full system message text from a ResponsesRequest.

    Concatenates all text parts from all SystemMessage items in the request.
    Useful for mocks that need to verify the system prompt contains expected content.
    """
    if isinstance(req.input, str):
        return ""

    parts: list[str] = []
    for item in req.input:
        if isinstance(item, SystemMessage):
            for part in item.content:
                if hasattr(part, "text"):
                    parts.append(part.text)
    return "\n".join(parts)


class SubprocessExecMock(MCPDecoratorMock):
    """Mock for in-container subprocess exec (DirectToolProvider).

    Uses plain tool name ``exec`` matching DirectToolProvider registration
    in in-container agent loops (critic, grader, PO/PI).

    For host-side docker exec via MCP server (editor_agent), use
    DockerExecMock from agent_core.testing.mcp.responses instead.
    """

    def exec_roundtrip(
        self, cmd: list[str], *, timeout_ms: int = 5000, cwd: str | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Yield exec call for in-container subprocess, return typed result."""
        exec_input = make_exec_input(cmd, timeout_ms=timeout_ms, cwd=cwd)
        call = self.tool_call("exec", exec_input)
        return tool_roundtrip(call, BaseExecResult)


class PropsMock(SubprocessExecMock):
    """Mock with props-specific helpers (psql, etc.)."""

    def psql_roundtrip(
        self, query: str, *, timeout_ms: int = 5000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Execute psql query via in-container exec and return result."""
        return self.exec_roundtrip(["psql", "-c", query], timeout_ms=timeout_ms)


def _extract_raw_output(req: ResponsesRequest, call: FunctionCallItem) -> str:
    """Extract raw string output for a function call from request."""
    output = one(
        item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id
    ).output
    if not isinstance(output, str):
        raise ValueError(f"Expected string output for call_id={call.call_id}, got list")
    return output


class GraderMock(DecoratorMock):
    """Mock with grader-specific tool helpers.

    Grader tools are registered directly (not via MCP), so they use simple names
    like 'list_pending', 'fill_remaining'.

    Example:
        @GraderMock.mock()
        def mock(m: GraderMock) -> PlayGen:
            yield None  # First request
            pending = yield from m.list_pending_roundtrip()
            for edge in pending:
                yield from m.fill_remaining_roundtrip(
                    edge.critique_run_id, edge.critique_issue_id, 1, "No matches"
                )
    """

    def list_pending_roundtrip(
        self, *, issue: str | None = None, run: UUID | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, list[PendingEdge]]:
        """Yield list_pending call and return parsed result as list of PendingEdge."""
        call = self.tool_call("list_pending", ListPendingArgs(issue=issue, run=run))
        req = yield call
        raw = _extract_raw_output(req, call)
        return TypeAdapter(list[PendingEdge]).validate_json(raw)

    def fill_remaining_roundtrip(
        self, run: UUID, issue_id: str, expected_count: int, rationale: str
    ) -> Generator[FunctionCallItem, ResponsesRequest, str]:
        """Yield fill_remaining call and return result message."""
        call = self.tool_call(
            "fill_remaining",
            FillRemainingArgs(run=run, issue_id=issue_id, expected_count=expected_count, rationale=rationale),
        )
        req = yield call
        return _extract_raw_output(req, call)
