"""CriticDev-specific mock utilities."""

from hamcrest import all_of, assert_that

from agent_core.testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully, stdout_contains
from openai_utils.model import FunctionCallItem
from props.agents.critic_dev.loop import ReportFailureArgs
from props.testing.mocks import SubprocessExecMock


class CriticDevMock(SubprocessExecMock):
    """Mock for critic-dev agents (optimizer, improver) with tool convenience methods.

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


def make_cli_test_mock(command: list[str], *, expected_output: str) -> CriticDevMock:
    """Create a mock that runs a CLI command, checks output, then terminates.

    Used by both optimize and improve test_e2e CLI helper tests.
    """

    @CriticDevMock.mock()
    def mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request
        result = yield from m.exec_roundtrip(command, timeout_ms=30000)
        assert_that(result, all_of(exited_successfully(), stdout_contains(expected_output)))
        yield m.report_failure(f"{command[1]} test completed")

    return mock
