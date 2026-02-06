"""CriticDev-specific mock utilities."""

from openai_utils.model import FunctionCallItem
from props.critic_dev.loop import ReportFailureArgs
from props.testing.mocks import PropsMock


class CriticDevMock(PropsMock):
    """Mock for critic-dev agents (prompt optimizer, improvement) with tool convenience methods.

    Example:
        @CriticDevMock.mock()
        def mock(m: CriticDevMock) -> PlayGen:
            yield None  # First request
            yield m.report_failure("Test complete")
    """

    def report_failure(self, message: str) -> FunctionCallItem:
        """Report that the task could not be completed."""
        return self.tool_call("report_failure", ReportFailureArgs(message=message))

    def report_success(self) -> FunctionCallItem:
        """Report that the task completed successfully."""
        return self.tool_call("report_success", {})
