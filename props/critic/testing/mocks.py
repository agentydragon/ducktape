"""Critic-specific mock utilities."""

from openai_utils.model import FunctionCallItem
from props.critic.main import InsertIssueArgs, InsertOccurrenceArgs, ReportFailureArgs, SubmitArgs
from props.testing.mocks import PropsMock


class CriticMock(PropsMock):
    """Mock for critic agent with tool convenience methods.

    Example:
        @CriticMock.mock()
        def mock(m: CriticMock) -> PlayGen:
            yield None  # First request
            yield m.insert_issue("issue-1", "Found dead code")
            yield m.insert_occurrence("issue-1", "foo.py", 1, 5)
            yield m.submit(issues_count=1, summary="Found 1 issue")
    """

    def insert_issue(self, issue_id: str, rationale: str) -> FunctionCallItem:
        """Insert a reported issue."""
        return self.tool_call("insert_issue", InsertIssueArgs(issue_id=issue_id, rationale=rationale))

    def insert_occurrence(
        self, issue_id: str, file: str, start_line: int | None = None, end_line: int | None = None
    ) -> FunctionCallItem:
        """Insert an occurrence for a reported issue."""
        return self.tool_call(
            "insert_occurrence",
            InsertOccurrenceArgs(issue_id=issue_id, file=file, start_line=start_line, end_line=end_line),
        )

    def submit(self, *, issues_count: int, summary: str) -> FunctionCallItem:
        """Submit the critique."""
        return self.tool_call("submit", SubmitArgs(issues_count=issues_count, summary=summary))

    def report_failure(self, message: str) -> FunctionCallItem:
        """Report that the critique could not be completed."""
        return self.tool_call("report_failure", ReportFailureArgs(message=message))
