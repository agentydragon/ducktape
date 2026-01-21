"""Props-specific mock utilities."""

import json
from collections.abc import Generator
from typing import Any

from pydantic import BaseModel

from agent_core_testing.responses import DecoratorMock
from mcp_infra.exec.models import BaseExecResult
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest


class PropsMock(DecoratorMock):
    """Mock with props-specific helpers (psql, etc.)."""

    def psql_roundtrip(
        self, query: str, *, timeout_ms: int = 5000
    ) -> Generator[FunctionCallItem, ResponsesRequest, BaseExecResult]:
        """Execute psql query via docker exec and return result."""
        return self.docker_exec_roundtrip(["psql", "-c", query], timeout_ms=timeout_ms)


def _extract_raw_output(req: ResponsesRequest, call: FunctionCallItem) -> str:
    """Extract raw string output for a function call from request."""
    matches = [item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly 1 output for call_id={call.call_id}, got {len(matches)}")
    output = matches[0].output
    if not isinstance(output, str):
        raise ValueError(f"Expected string output for call_id={call.call_id}, got list")
    return output


class GraderMock(DecoratorMock):
    """Mock with grader-specific tool helpers.

    Grader tools are registered directly (not via MCP), so they use simple names
    like 'list_pending', 'fill_remaining', 'submit'.

    Example:
        @GraderMock.mock()
        def mock(m: GraderMock) -> PlayGen:
            yield None  # First request
            pending = yield from m.list_pending_roundtrip()
            for item in pending:
                issue_id = item["critique_issue_id"]
                run_id = item["critique_run_id"]
                yield from m.fill_remaining_roundtrip(run_id, issue_id, 1, "No matches")
            yield m.submit("Grading complete")
    """

    def grader_tool_call(self, name: str, arguments: dict[str, Any] | BaseModel) -> FunctionCallItem:
        """Create a grader tool call (tools registered directly, no MCP prefix)."""
        args_dict = arguments.model_dump(mode="json") if isinstance(arguments, BaseModel) else arguments
        return self.tool_call(name, args_dict)

    def list_pending_call(self, *, issue: str | None = None, run: str | None = None) -> FunctionCallItem:
        """Create list_pending tool call."""
        args: dict[str, Any] = {}
        if issue is not None:
            args["issue"] = issue
        if run is not None:
            args["run"] = run
        return self.grader_tool_call("list_pending", args)

    def list_pending_roundtrip(
        self, *, issue: str | None = None, run: str | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, list[dict[str, Any]]]:
        """Yield list_pending call and return parsed result as list of dicts."""
        call = self.list_pending_call(issue=issue, run=run)
        req = yield call
        raw = _extract_raw_output(req, call)
        result: list[dict[str, Any]] = json.loads(raw)
        return result

    def fill_remaining_call(self, run: str, issue_id: str, expected_count: int, rationale: str) -> FunctionCallItem:
        """Create fill_remaining tool call."""
        return self.grader_tool_call(
            "fill_remaining",
            {"run": run, "issue_id": issue_id, "expected_count": expected_count, "rationale": rationale},
        )

    def fill_remaining_roundtrip(
        self, run: str, issue_id: str, expected_count: int, rationale: str
    ) -> Generator[FunctionCallItem, ResponsesRequest, str]:
        """Yield fill_remaining call and return result message."""
        call = self.fill_remaining_call(run, issue_id, expected_count, rationale)
        req = yield call
        return _extract_raw_output(req, call)

    def submit_call(self, summary: str) -> FunctionCallItem:
        """Create submit tool call."""
        return self.grader_tool_call("submit", {"summary": summary})

    def submit(self, summary: str) -> FunctionCallItem:
        """Create submit tool call (alias for convenience)."""
        return self.submit_call(summary)
