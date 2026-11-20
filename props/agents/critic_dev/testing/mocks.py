"""CriticDev-specific mock utilities."""

from collections.abc import Generator
from uuid import UUID

from openai_utils.model import FunctionCallItem, ResponsesRequest
from openai_utils.testing.responses import tool_roundtrip
from props.agents.critic_dev.loop import ReportFailureArgs, WaitUntilCriticCompletedArgs, WaitUntilGradedToolArgs
from props.core.eval_api_models import CriticRunStatus, GradingStatusResponse, RunCriticRequest, StartCriticResponse
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import WholeSnapshotExample
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

    def start_critic_roundtrip(
        self, request: RunCriticRequest
    ) -> Generator[FunctionCallItem, ResponsesRequest, StartCriticResponse]:
        """Yield start_critic tool call, return StartCriticResponse."""
        return tool_roundtrip(self.tool_call("start_critic", request), StartCriticResponse)

    def wait_until_critic_completed_roundtrip(
        self, critic_run_id: UUID, *, timeout_seconds: int
    ) -> Generator[FunctionCallItem, ResponsesRequest, CriticRunStatus]:
        """Yield wait_until_critic_completed tool call, return CriticRunStatus."""
        args = WaitUntilCriticCompletedArgs(critic_run_id=critic_run_id, timeout_seconds=timeout_seconds)
        return tool_roundtrip(self.tool_call("wait_until_critic_completed", args), CriticRunStatus)

    def wait_until_graded_roundtrip(
        self, critic_run_id: UUID, *, timeout_seconds: int = 300
    ) -> Generator[FunctionCallItem, ResponsesRequest, GradingStatusResponse]:
        """Yield wait_until_graded_tool call, return GradingStatusResponse."""
        args = WaitUntilGradedToolArgs(critic_run_id=critic_run_id, timeout_seconds=timeout_seconds)
        return tool_roundtrip(self.tool_call("wait_until_graded_tool", args), GradingStatusResponse)

    def evaluate_critic_roundtrip(
        self, *, definition_id: DefinitionId, snapshot_slug: SnapshotSlug, critic_model: str
    ) -> Generator[FunctionCallItem, ResponsesRequest]:
        """Start a critic on the whole snapshot, then wait until it has exited and its issues are graded."""
        started = yield from self.start_critic_roundtrip(
            RunCriticRequest(
                definition_id=definition_id,
                example=WholeSnapshotExample(snapshot_slug=snapshot_slug),
                timeout_seconds=120,
                budget_usd=1.0,
                critic_model=critic_model,
            )
        )
        yield from self.wait_until_critic_completed_roundtrip(started.critic_run_id, timeout_seconds=120)
        yield from self.wait_until_graded_roundtrip(started.critic_run_id, timeout_seconds=60)
