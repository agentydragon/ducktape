"""E2E test: critic-dev agent executes bundled recipe modules in its container.

Tests that recipe modules bundled into the critic-dev OCI image are importable
and produce correct results when run inside the agent container against the
synced test database.

Data seeding: test_train_example_with_runs creates critic+grader runs with
grading edges so recall views and run_analysis have non-trivial data.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from typing import Any

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully
from mcp_infra.exec.models import BaseExecResult
from openai_utils.model import FunctionCallItem, ResponsesRequest
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.core.agent_types import TargetMetric
from props.db.models import AgentRun
from props.testing.constants import DEFAULT_TEST_MODEL

logger = logging.getLogger(__name__)


RECIPE_PKG = "props.agents.critic_dev.recipes"


def _run_recipe(
    m: CriticDevMock, module: str, *args: str
) -> Generator[FunctionCallItem, ResponsesRequest, dict[str, Any]]:
    """Run a recipe's main() in the container and return parsed JSON output."""
    arg_str = ", ".join(repr(a) for a in args)
    code = f"from {RECIPE_PKG}.{module} import main; main({arg_str})"
    result: BaseExecResult = yield from m.exec_roundtrip(["python3", "-c", code], timeout_ms=30000)
    assert_that(result, exited_successfully())
    assert isinstance(result.stdout, str), f"{module} output was truncated"
    data: dict[str, Any] = json.loads(result.stdout.strip().split("\n")[-1])
    return data


@pytest.mark.timeout(180)
async def test_recipes_in_container(
    synced_db, e2e_stack, test_snapshot, test_train_example_with_runs, critic_dev_optimize_image, critic_image
):
    """Verify bundled recipe modules work inside the critic-dev container.

    The mock LLM executes each recipe's main() inside the container, then
    verifies the JSON output contains expected data from the synced test DB.
    test_train_example_with_runs seeds critic+grader runs with grading edges
    so recall_metrics and run_analysis produce non-trivial output.
    """
    snapshot = str(test_snapshot)

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        gt_data = yield from _run_recipe(m, "ground_truth", snapshot)
        assert len(gt_data["train_snapshots"]) >= 1
        assert any(s["slug"] == snapshot for s in gt_data["train_snapshots"])
        assert len(gt_data["true_positives"]) >= 1
        assert all(tp["num_occurrences"] >= 1 for tp in gt_data["true_positives"])
        logger.info(f"ground_truth: {gt_data}")

        ex_data = yield from _run_recipe(m, "examples_and_scopes")
        assert len(ex_data["train_examples"]) >= 1
        assert all(e["recall_denominator"] >= 0 for e in ex_data["train_examples"])
        logger.info(f"examples_and_scopes: {ex_data}")

        rm_data = yield from _run_recipe(m, "recall_metrics")
        assert len(rm_data["leaderboard"]) >= 1, f"Expected non-empty leaderboard: {rm_data}"
        assert all(row["recall_denominator"] >= 1 for row in rm_data["leaderboard"])
        logger.info(f"recall_metrics: {rm_data}")

        ra_data = yield from _run_recipe(m, "run_analysis", snapshot)
        assert len(ra_data["recent_runs"]) >= 1, f"Expected non-empty runs: {ra_data}"
        logger.info(f"run_analysis: {ra_data}")

        yield m.report_failure("Recipe verification done — exiting via report_failure")

    async with e2e_stack(
        {DEFAULT_TEST_MODEL: optimizer_mock}, images=[critic_dev_optimize_image, critic_image]
    ) as stack:
        run_id = await stack.registry.run_critic_dev_optimize(
            image=stack.resolved_images["critic_dev_optimize"],
            budget=50.0,
            optimizer_model=stack.model,
            critic_model=stack.model,
            target_metric=TargetMetric.WHOLE_REPO,
            timeout_seconds=120,
        )

    with synced_db.session() as session:
        optimizer_run = session.get(AgentRun, run_id)
        assert optimizer_run is not None


if __name__ == "__main__":
    pytest_bazel.main()
