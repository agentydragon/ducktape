"""Grader-specific mock utilities."""

from collections.abc import Generator
from uuid import UUID

from more_itertools import one

from agent_core.testing.responses import DecoratorMock, _extract_text_from_content_list, tool_roundtrip
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest
from props.agents.grader.tools import (
    EdgeSpec,
    FillRemainingArgs,
    InsertEdgesArgs,
    ListPendingArgs,
    PendingEdge,
    SleepArgs,
)


def _extract_raw_output(req: ResponsesRequest, call: FunctionCallItem) -> str:
    """Extract raw string output for a function call from request."""
    output = one(
        item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id
    ).output
    if isinstance(output, str):
        return output
    return _extract_text_from_content_list(output, call.call_id)


class GraderMock(DecoratorMock):
    """Mock with grader-specific tool helpers.

    Grader tools are registered directly (not via MCP), so they use simple names
    like 'list_pending', 'fill_remaining', 'insert_edges'.

    Example:
        @GraderMock.mock()
        def mock(m: GraderMock) -> PlayGen:
            yield None  # First request
            pending = yield from m.list_pending_roundtrip()
            for edge in pending:
                yield from m.fill_remaining_roundtrip(
                    edge.critique_run_id, edge.critique_issue_id, 1, "No matches"
                )
            yield m.sleep("Graded all pending edges")
    """

    def sleep(self, summary: str) -> FunctionCallItem:
        """Signal that grading is complete and daemon should sleep."""
        return self.tool_call("sleep", SleepArgs(summary=summary))

    def list_pending_roundtrip(
        self, *, issue: str | None = None, run: UUID | None = None
    ) -> Generator[FunctionCallItem, ResponsesRequest, list[PendingEdge]]:
        """Yield list_pending call and return parsed result as list of PendingEdge."""
        return (
            yield from tool_roundtrip(
                self.tool_call("list_pending", ListPendingArgs(issue=issue, run=run)), list[PendingEdge]
            )
        )

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

    def insert_edges_roundtrip(
        self, run: UUID, issue_id: str, edges: list[EdgeSpec], rationale: str
    ) -> Generator[FunctionCallItem, ResponsesRequest, str]:
        """Yield insert_edges call and return result message."""
        call = self.tool_call(
            "insert_edges", InsertEdgesArgs(run=run, issue_id=issue_id, edges=edges, rationale=rationale)
        )
        req = yield call
        return _extract_raw_output(req, call)
