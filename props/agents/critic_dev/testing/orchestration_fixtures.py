"""Shared constants and mocks for multi-model orchestration e2e tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from props.core.agent_types import TargetMetric
from props.core.ids import SnapshotSlug
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
