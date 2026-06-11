"""E2E test fixtures for props tests.

For in-container e2e tests, see e2e_container.py which provides the full stack:
- FakeOpenAIServer with scripted responses
- LLM proxy for auth validation and request logging
- AgentRegistry for container orchestration
"""

from collections.abc import Callable

import pytest

from openai_utils.testing.openai_mock import FakeOpenAIModel
from openai_utils.model import ResponsesResult
from props.core.ids import SnapshotSlug
from props.critic_dev.improve.main import TerminationSuccess


@pytest.fixture
def mock_snapshot_slug() -> SnapshotSlug:
    """Shared test snapshot slug."""
    return SnapshotSlug("ducktape/2025-11-26-00")


@pytest.fixture
def noop_openai_client() -> FakeOpenAIModel:
    """Mock OpenAI client with no responses - for unused critic/grader clients."""
    return FakeOpenAIModel(outputs=[])


@pytest.fixture
def make_openai_client() -> Callable[[list[ResponsesResult]], FakeOpenAIModel]:
    """Factory fixture for creating mock OpenAI clients from response sequences."""

    def _factory(responses: list[ResponsesResult]) -> FakeOpenAIModel:
        return FakeOpenAIModel(responses)

    return _factory


@pytest.fixture
def success_termination() -> TerminationSuccess:
    return TerminationSuccess(definition_id="test-improved-critic", total_credit=2.0, baseline_avg=1.0)


# NOTE: run_prompt_optimizer_with_steps and run_improvement_agent_with_steps fixtures
# have been removed - they used the old direct-call architecture where mock clients
# could be passed directly. Use e2e_container.py fixtures for the new in-container
# architecture instead.
