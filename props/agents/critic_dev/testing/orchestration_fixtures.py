"""Shared constants and mocks for multi-model orchestration e2e tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from openai_utils.model import Model
from openai_utils.testing.responses import PlayGen
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.core.agent_types import TargetMetric
from props.core.ids import DefinitionId, SnapshotSlug
from props.testing.fixtures.e2e_container import E2EStack

# Model names for multi-model routing.
# Must exist in synced model_metadata (llm_requests.model has FK to model_metadata.model_id).
# Each must be DISTINCT so multi-model FakeOpenAIServer routes to the right mock.
ORCHESTRATION_OPTIMIZER_MODEL = "gpt-4o"
ORCHESTRATION_CRITIC_MODEL = "gpt-4o-mini"
ORCHESTRATION_GRADER_MODEL = "gpt-4.1-mini"


@asynccontextmanager
async def critic_dev_optimizer(stack: E2EStack, snapshot_slug: SnapshotSlug) -> AsyncIterator[UUID]:
    """Run the optimizer to completion while a snapshot grader runs in the background; yield its run ID."""
    grader = await stack.registry.start_snapshot_grader(
        image=stack.resolved_images["grader"], snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL
    )
    async with grader:
        run_id = await stack.registry.run_critic_dev_optimize(
            image=stack.resolved_images["critic_dev_optimize"],
            budget=1.0,
            optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
            critic_model=ORCHESTRATION_CRITIC_MODEL,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=180,
        )
        yield run_id


@pytest.fixture
def run_optimizer_critic(e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image):
    """Evaluate the pushed critic with the scenario's critic and grader scripts."""

    @asynccontextmanager
    async def run(critic: Model, grader: Model) -> AsyncIterator[UUID]:
        @CriticDevMock.mock()
        def optimizer(m: CriticDevMock) -> PlayGen:
            yield None
            yield from m.evaluate_critic_roundtrip(
                definition_id=DefinitionId(stack.resolved_images["critic"].digest),
                snapshot_slug=test_snapshot,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
            )
            yield m.report_success()

        mocks = {
            ORCHESTRATION_OPTIMIZER_MODEL: optimizer,
            ORCHESTRATION_CRITIC_MODEL: critic,
            ORCHESTRATION_GRADER_MODEL: grader,
        }
        async with (
            e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack,
            critic_dev_optimizer(stack, test_snapshot) as run_id,
        ):
            yield run_id

    return run
