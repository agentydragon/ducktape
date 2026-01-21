"""E2E test fixtures (agent runners, registries) for props tests.

NOTE: Several fixtures in this file are for the old direct-call architecture
where mock clients could be passed directly to run_* functions. The new
in-container architecture uses AgentRegistry methods with llm_proxy_url and
containers that talk to an LLM proxy. See e2e_container.py for the new approach.

TODO: Migrate these fixtures to the in-container architecture.
"""

from collections.abc import Callable
from uuid import UUID

import pytest
import pytest_asyncio

from agent_core_testing.openai_mock import FakeOpenAIModel
from agent_core_testing.steps import Step
from openai_utils.model import ResponsesResult
from props.core.agent_registry import AgentRegistry
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.models import AgentRun, AgentRunStatus
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.core.prompt_improve.main import TerminationSuccess
from props.core.prompt_optimize.target_metric import TargetMetric

# Default timeout for tests (10 minutes)
TEST_TIMEOUT_SECONDS = 600


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
def run_critic_with_steps(synced_test_db, test_snapshot, make_step_runner, async_docker_client):
    """Factory fixture for running critic with custom steps."""

    async def _run(
        steps: list[Step],
        *,
        image_ref: str = CRITIC_IMAGE_REF,
        example: ExampleSpec | None = None,
        model: str = "gpt-4o",
    ) -> tuple[UUID, AgentRunStatus, object]:
        if example is None:
            example = WholeSnapshotExample(snapshot_slug=test_snapshot)

        runner = make_step_runner(steps=steps)
        # TODO: This fixture needs updating for in-container architecture
        # Using placeholder llm_proxy_url - real tests should use e2e_container.py fixtures
        registry = AgentRegistry(
            docker_client=async_docker_client,
            db_config=synced_test_db,
            llm_proxy_url="http://placeholder:8080",
        )
        try:
            critic_run_id = await registry.run_critic(
                image_ref=image_ref,
                example=example,
                model=model,
                timeout_seconds=TEST_TIMEOUT_SECONDS,
                parent_run_id=None,
                budget_usd=None,
            )
            with get_session() as session:
                critic_run = session.get(AgentRun, critic_run_id)
                assert critic_run is not None
                status = critic_run.status
            return critic_run_id, status, runner
        finally:
            await registry.close()

    return _run


@pytest_asyncio.fixture
async def test_registry(synced_test_db, async_docker_client):
    """Provide AgentRegistry for tests, handling cleanup.

    TODO: This fixture needs updating for in-container architecture.
    Using placeholder llm_proxy_url - real tests should use e2e_container.py fixtures.
    """
    registry = AgentRegistry(
        docker_client=async_docker_client,
        db_config=synced_test_db,
        llm_proxy_url="http://placeholder:8080",
    )
    yield registry
    await registry.close()


@pytest.fixture
def success_termination() -> TerminationSuccess:
    return TerminationSuccess(definition_id="test-improved-critic", total_credit=2.0, baseline_avg=1.0)


# NOTE: run_prompt_optimizer_with_steps and run_improvement_agent_with_steps fixtures
# have been removed - they used the old direct-call architecture where mock clients
# could be passed directly. Use e2e_container.py fixtures for the new in-container
# architecture instead.
