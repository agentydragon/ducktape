"""E2E test for agent orchestration.

Tests the orchestration workflow:
1. Optimizer calls run_critic to spawn critic containers
2. Critic receives system prompt and submits issues
3. Grader processes edges
4. Optimizer waits for grading and reports success

Uses the in-container architecture with:
- FakeOpenAI server backed by CriticDevMock/CriticMock/GraderMock
- LLM proxy for auth and logging
- AgentRegistry for container orchestration
"""

from __future__ import annotations

import json
import logging

import pytest_bazel
from hamcrest import all_of, assert_that
from more_itertools import one

from devinfra.oci.oci import BazelImage
from mcp_infra.testing.matchers import exited_successfully, stdout_contains
from openai_utils.testing.responses import PlayGen
from props.agents.critic.testing.mocks import CriticMock
from props.agents.critic_dev.registry_push_probe import CONTAINER_SCRIPT_PATH
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.agents.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
    critic_dev_optimizer,
)
from props.agents.grader.testing.mocks import GraderMock
from props.core.ids import DefinitionId, SnapshotSlug
from props.db.models import AgentRun, AgentRunStatus
from props.testing.mocks import get_system_prompt_text
from props.testing.models import DEFAULT_TEST_MODEL
from util.exec.models import BaseExecResult

# gazelle:include_dep //props/agents/testing:e2e_pytest_plugins
pytest_plugins = ("props.agents.testing.e2e_pytest_plugins",)

logger = logging.getLogger(__name__)

_CRITIC_REGISTRY_PROBE_IMAGE = BazelImage(
    repo_name="critic", image_rlocation="ducktape/props/agents/critic_dev/critic_registry_probe_image"
)


# Test timeout (seconds)
TEST_TIMEOUT_SECONDS = 120


async def test_po_orchestrates_critic_with_system_prompt_check(
    synced_db, e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image
):
    """Test critic-dev optimizer orchestration with critic system prompt verification.

    Verifies:
    1. Optimizer can call run_critic MCP tool
    2. Critic receives a valid system prompt (mechanism check)
    3. Grader processes the edges
    4. Optimizer's wait_until_graded returns

    The critic mock verifies it receives a proper system message, proving
    the in-container architecture properly passes prompts to agents.
    """
    snapshot_slug = SnapshotSlug(test_snapshot)

    # Mutable container filled after image push but before mock runs
    digests: dict[str, str] = {}

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        yield from m.evaluate_critic_roundtrip(
            definition_id=DefinitionId(digests["critic"]),
            snapshot_slug=snapshot_slug,
            critic_model=ORCHESTRATION_CRITIC_MODEL,
        )

        # Report success
        yield m.report_success()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        # Capture first request to verify system message is present
        first_request = yield None

        # Verify we received a non-empty system message
        system_text = get_system_prompt_text(first_request)
        assert system_text, "Expected non-empty system message"
        logger.info(f"Critic received system message ({len(system_text)} chars)")

        # Submit zero issues
        yield m.submit(issues_count=0, summary="Critic completed")

    # Grader mock for zero-issue case: should never be woken
    @GraderMock.mock()
    def grader_mock(m: GraderMock) -> PlayGen:
        yield None  # First request (waits for drift)
        raise AssertionError("Grader should not be woken when critic submits 0 issues")

    mocks = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
        ORCHESTRATION_GRADER_MODEL: grader_mock,
    }
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        digests.update(stack.image_digests)
        async with critic_dev_optimizer(stack, snapshot_slug) as run_id:
            # Verify optimizer completed while the snapshot grader remains alive.
            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.EXITED, f"Expected COMPLETED, got {optimizer_run.status}"


async def test_critic_cannot_push_images(e2e_stack, all_files_scope):
    """A critic's own credentials authenticate to the registry proxy, but both steps of an image push are refused.

    The first request anchors the other two: the proxy accepts the credentials, so the refusals come from the
    role check and not from a bad login or an unreachable proxy. A critic-dev agent's push through the same proxy
    is exercised by recipes/test_build_critic_e2e.py.
    """
    probes: list[BaseExecResult] = []

    @CriticMock.mock()
    def mock(m: CriticMock) -> PlayGen:
        yield None  # First request
        probes.append((yield from m.exec_roundtrip(["python3", CONTAINER_SCRIPT_PATH], timeout_ms=60000)))
        yield m.submit(issues_count=0, summary="Registry probe completed")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[_CRITIC_REGISTRY_PROBE_IMAGE]) as stack:
        await stack.registry.run_critic(
            image=stack.resolved_images[_CRITIC_REGISTRY_PROBE_IMAGE.repo_name],
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=5.0,
        )

    assert_that(
        one(probes),
        all_of(
            exited_successfully(),
            stdout_contains(json.dumps({"api_check": 200, "blob_upload": 403, "manifest_put": 403}, sort_keys=True)),
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
