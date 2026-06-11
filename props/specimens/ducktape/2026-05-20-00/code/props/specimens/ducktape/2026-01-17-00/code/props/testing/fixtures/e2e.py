"""E2E test fixtures (agent runners, registries) for props tests."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio

from agent_core_testing.openai_mock import FakeOpenAIModel
from agent_core_testing.steps import Step
from openai_utils.model import ResponsesResult
from props.core.agent_registry import AgentRegistry
from props.core.agent_workspace import WorkspaceManager
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.models import AgentRun, AgentRunStatus
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, WholeSnapshotExample
from props.core.prompt_improve.improve_agent import run_improvement_agent
from props.core.prompt_improve.reminder_handler import TerminationSuccess
from props.core.prompt_optimize.prompt_optimizer import run_prompt_optimizer
from props.core.prompt_optimize.target_metric import TargetMetric


@pytest.fixture
def test_workspace_manager(tmp_path: Path) -> WorkspaceManager:
    """Shared WorkspaceManager fixture for agent tests."""
    return WorkspaceManager(tmp_path)


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
def run_critic_with_steps(synced_test_db, test_snapshot, make_step_runner, async_docker_client, test_workspace_manager):
    """Factory fixture for running critic with custom steps."""

    async def _run(
        steps: list[Step],
        *,
        image_ref: str = CRITIC_IMAGE_REF,
        example: ExampleSpec | None = None,
        max_turns: int = 100,
    ) -> tuple[UUID, AgentRunStatus, object]:
        if example is None:
            example = WholeSnapshotExample(snapshot_slug=test_snapshot)

        runner = make_step_runner(steps=steps)
        registry = AgentRegistry(
            docker_client=async_docker_client, db_config=synced_test_db, workspace_manager=test_workspace_manager
        )
        try:
            critic_run_id = await registry.run_critic(
                image_ref=image_ref, example=example, client=runner, max_turns=max_turns
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
async def test_registry(synced_test_db, async_docker_client, test_workspace_manager):
    """Provide AgentRegistry for tests, handling cleanup."""
    registry = AgentRegistry(
        docker_client=async_docker_client, db_config=synced_test_db, workspace_manager=test_workspace_manager
    )
    yield registry
    await registry.close()


@pytest.fixture
def run_prompt_optimizer_with_steps(synced_test_db, make_step_runner, make_openai_client, async_docker_client):
    """Factory fixture for running prompt optimizer with custom steps."""

    async def _run(
        steps: list[Step],
        *,
        critic_steps: list[Step] | None = None,
        grader_steps: list[Step] | None = None,
        budget: float = 1.0,
        target_metric: TargetMetric = TargetMetric.WHOLE_REPO,
    ) -> None:
        runner = make_step_runner(steps=steps)
        critic_client = make_step_runner(steps=critic_steps) if critic_steps else make_openai_client([])
        grader_client = make_step_runner(steps=grader_steps) if grader_steps else make_openai_client([])

        await run_prompt_optimizer(
            budget=budget,
            optimizer_client=runner,
            critic_client=critic_client,
            grader_client=grader_client,
            docker_client=async_docker_client,
            target_metric=target_metric,
            db_config=synced_test_db,
        )

    return _run


@pytest.fixture
def success_termination() -> TerminationSuccess:
    return TerminationSuccess(definition_id="test-improved-critic", total_credit=2.0, baseline_avg=1.0)


@pytest.fixture
def run_improvement_agent_with_steps(
    synced_test_db,
    make_step_runner,
    async_docker_client,
    success_termination,
    subtract_file_example,
    noop_openai_client,
):
    """Factory fixture for running improvement agent with custom steps."""

    async def _run(
        steps: list[Step],
        *,
        token_budget: int = 100_000,
        model: str = "gpt-5-nano",
        baseline_image_refs: list[str] | None = None,
    ):
        if baseline_image_refs is None:
            baseline_image_refs = [CRITIC_IMAGE_REF]

        runner = make_step_runner(steps=steps)

        def mock_check_termination(session, improvement_run_id, type_config):
            return success_termination

        with patch(
            "props.prompt_improve.reminder_handler.check_termination_condition", side_effect=mock_check_termination
        ):
            return await run_improvement_agent(
                examples=[subtract_file_example],
                baseline_image_refs=baseline_image_refs,
                token_budget=token_budget,
                model=model,
                docker_client=async_docker_client,
                db_config=synced_test_db,
                client=runner,
                critic_client=noop_openai_client,
                grader_client=noop_openai_client,
            )

    return _run
