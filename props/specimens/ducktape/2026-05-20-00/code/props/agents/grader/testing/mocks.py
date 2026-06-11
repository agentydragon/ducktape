"""Grader-specific mock utilities."""

from collections.abc import Generator
from typing import NoReturn
from uuid import UUID

from more_itertools import one

from agent_core.testing.responses import DecoratorMock, tool_roundtrip
from openai_utils.model import FunctionCallItem, FunctionCallOutputItem, ResponsesRequest
from props.agents.grader.drift_handler import Drift
from props.agents.grader.tools import (
    ClusterMemberSpec,
    CreateClusterArgs,
    EdgeSpec,
    FillRemainingArgs,
    InsertEdgesArgs,
    ReportFailureArgs,
    SleepArgs,
)


def _extract_raw_output(req: ResponsesRequest, call: FunctionCallItem) -> str:
    """Extract raw string output for a function call from request."""
    return one(
        item for item in req.input if isinstance(item, FunctionCallOutputItem) and item.call_id == call.call_id
    ).output


class GraderMock(DecoratorMock):
    """Mock with grader-specific tool helpers.

    Grader tools are registered directly (not via MCP), so they use simple names
    like 'get_drift', 'fill_remaining', 'insert_edges'.

    Example:
        @GraderMock.mock()
        def mock(m: GraderMock) -> PlayGen:
            yield None  # First request
            drift = yield from m.get_drift_roundtrip()
            for edge in drift.grading:
                yield from m.fill_remaining_roundtrip(
                    edge.critique_run_id, edge.critique_issue_id, 1, "No matches"
                )
            yield from m.sleep_forever("Graded all pending edges")
    """

    def report_failure(self, message: str) -> FunctionCallItem:
        """Return a report_failure tool call item (terminal - grader exits after this)."""
        return self.tool_call("report_failure", ReportFailureArgs(message=message))

    def sleep(self, summary: str) -> FunctionCallItem:
        """Signal that grading is complete and grader should sleep."""
        return self.tool_call("sleep", SleepArgs(summary=summary))

    def get_drift_roundtrip(self) -> Generator[FunctionCallItem, ResponsesRequest, Drift]:
        """Yield get_drift call and return parsed Drift result."""
        return (yield from tool_roundtrip(self.tool_call("get_drift", {}), Drift))

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

    def create_cluster_roundtrip(
        self, cluster_id: str, rationale: str, members: list[ClusterMemberSpec]
    ) -> Generator[FunctionCallItem, ResponsesRequest, str]:
        """Yield create_cluster call and return result message."""
        call = self.tool_call(
            "create_cluster", CreateClusterArgs(cluster_id=cluster_id, rationale=rationale, members=members)
        )
        req = yield call
        return _extract_raw_output(req, call)

    def check_no_drift(self) -> Generator[FunctionCallItem, ResponsesRequest]:
        """Assert no pending work (grading or clustering)."""
        drift = yield from self.get_drift_roundtrip()
        assert not drift.has_pending, f"Expected no pending work: {drift!r}"

    def sleep_forever(self, summary: str) -> Generator[FunctionCallItem, ResponsesRequest, NoReturn]:
        """Assert no pending work, sleep, and fail if woken up.

        Use when the mock expects grading to be complete and the grader
        should sleep indefinitely (until cancelled).
        """
        yield from self.check_no_drift()
        yield self.sleep(summary)
        raise AssertionError("Grader woke up unexpectedly after sleeping")
