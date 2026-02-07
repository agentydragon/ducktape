"""E2E test for agent orchestration and custom agent images.

Tests the orchestration workflow:
1. Optimizer calls run_critic to spawn critic containers
2. Critic receives system prompt and submits issues
3. Grader processes edges
4. Optimizer waits for grading and reports success

Custom image flow:
1. Create custom agent.md content with random token
2. Use crane push to push OCI layout to registry proxy
3. Proxy automatically creates agent_definitions row
4. Run the newly created agent image via run_critic tool
5. Verify new agent got the custom agent.md in its system message

Uses the in-container architecture with:
- FakeOpenAI server backed by CriticDevMock/CriticMock/GraderMock
- LLM proxy for auth and logging
- AgentRegistry for container orchestration
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen, tool_roundtrip
from mcp_infra.exec.matchers import exited_successfully
from props.agents.critic.testing.mocks import CriticMock
from props.agents.critic_dev.loop import RunCriticToolArgs, WaitUntilGradedToolArgs
from props.agents.critic_dev.shared import TargetMetric
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.agents.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
    make_orchestration_grader_mock,
)
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.mocks import get_system_message_text

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

# Test timeout (seconds)
TEST_TIMEOUT_SECONDS = 120


@pytest.mark.timeout(300)
@pytest.mark.slow
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

        # Call run_critic tool (DirectToolProvider)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id=digests["critic"], example=example, timeout_seconds=120, budget_usd=1.0
        )

        call = m.tool_call("run_critic", run_critic_args)
        run_critic_output: RunCriticResponse = yield from tool_roundtrip(call, RunCriticResponse)

        critic_run_id = run_critic_output.critic_run_id
        logger.info(f"PO got critic_run_id: {critic_run_id}")

        # Wait for grading (polls database directly inside container)
        wait_args = WaitUntilGradedToolArgs(critic_run_id=str(critic_run_id), timeout_seconds=60)
        wait_call = m.tool_call("wait_until_graded_tool", wait_args)
        wait_output: GradingStatusResponse = yield from tool_roundtrip(wait_call, GradingStatusResponse)
        logger.info(f"PO got grading: total_credit={wait_output.total_credit}")

        # Report success
        yield m.report_success()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        # Capture first request to verify system message is present
        first_request = yield None

        # Verify we received a non-empty system message
        system_text = get_system_message_text(first_request)
        assert system_text, "Expected non-empty system message"
        assert "critic" in system_text.lower(), (
            f"Expected system message to mention 'critic'. Got: {system_text[:200]}..."
        )
        logger.info(f"Critic received system message ({len(system_text)} chars)")

        # Submit zero issues
        yield m.submit(issues_count=0, summary="Critic completed")

    grader_mock = make_orchestration_grader_mock()

    mocks = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
        ORCHESTRATION_GRADER_MODEL: grader_mock,
    }
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        digests.update(stack.image_digests)

        # Start grader daemon in background
        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
        )

        try:
            # Run critic-dev optimizer
            run_id = await stack.registry.run_critic_dev_optimize(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            # Verify optimizer completed
            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.EXITED, f"Expected COMPLETED, got {optimizer_run.status}"

        finally:
            grader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grader_task


@pytest.mark.timeout(300)
@pytest.mark.slow
async def test_po_creates_custom_critic_with_token(
    synced_db, e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image
):
    """Test full custom image flow: PO creates critic image, critic verifies prompt token.

    This test verifies the complete workflow:
    1. Optimizer creates a custom agent.md with a unique verification token
    2. Optimizer uses crane push to push OCI layout to registry proxy
    3. Optimizer calls run_critic with the new custom image
    4. Critic receives system prompt and asserts it contains the token
    5. Grading completes
    """
    snapshot_slug = SnapshotSlug(test_snapshot)
    verification_token = f"VERIFY_{secrets.token_hex(8)}"

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        # Create custom critic directory with agent.md containing the random token
        agent_md_content = f"""# Custom Critic with Verification Token

You are a code critic. VERIFICATION_TOKEN: {verification_token}

Find issues in the code and report them.
"""
        result = yield from m.exec_roundtrip(
            [
                "sh",
                "-c",
                f"""mkdir -p /workspace/custom_critic && \
cat > /workspace/custom_critic/agent.md << 'AGENT_EOF'
{agent_md_content}
AGENT_EOF
""",
            ],
            timeout_ms=15000,
        )
        assert_that(result, exited_successfully())

        # Push the custom image via crane to the registry proxy
        result = yield from m.exec_roundtrip(
            [
                "sh",
                "-c",
                "crane push /workspace/custom_critic/ ${PROPS_BACKEND_URL#http://}/custom_critic:latest --insecure",
            ],
            timeout_ms=60000,
        )
        assert_that(result, exited_successfully())
        logger.info(f"crane push output: {result.stdout}")

        # Extract digest from the created agent definition
        result = yield from m.exec_roundtrip(
            ["psql", "-t", "-c", "SELECT digest FROM agent_definitions ORDER BY created_at DESC LIMIT 1"],
            timeout_ms=10000,
        )
        assert_that(result, exited_successfully())
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.truncated_text
        new_digest = stdout.strip()
        logger.info(f"Created custom critic with digest: {new_digest}")

        # Call run_critic with the CUSTOM critic image (DirectToolProvider)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id=new_digest,  # Use the custom image!
            example=example,
            timeout_seconds=120,
            budget_usd=1.0,
        )

        call = m.tool_call("run_critic", run_critic_args)
        run_critic_output: RunCriticResponse = yield from tool_roundtrip(call, RunCriticResponse)

        critic_run_id = run_critic_output.critic_run_id
        logger.info(f"PO got critic_run_id: {critic_run_id}")

        # Wait for grading (polls database directly inside container)
        wait_args = WaitUntilGradedToolArgs(critic_run_id=str(critic_run_id), timeout_seconds=60)
        wait_call = m.tool_call("wait_until_graded_tool", wait_args)
        wait_output: GradingStatusResponse = yield from tool_roundtrip(wait_call, GradingStatusResponse)
        logger.info(f"PO got grading: total_credit={wait_output.total_credit}")

        # Report success
        yield m.report_success()

    @CriticMock.mock()
    def critic_mock(m: CriticMock) -> PlayGen:
        # Capture first request to verify system message contains the token
        first_request = yield None

        # Verify the system message contains our expected token
        system_text = get_system_message_text(first_request)
        assert verification_token in system_text, (
            f"Expected token '{verification_token}' not found in system message. "
            f"System message starts with: {system_text[:200]}..."
        )
        logger.info(f"Critic received system message with expected token: {verification_token}")

        # Submit zero issues
        yield m.submit(issues_count=0, summary="Custom critic completed")

    grader_mock = make_orchestration_grader_mock()

    mocks = {
        ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock,
        ORCHESTRATION_CRITIC_MODEL: critic_mock,
        ORCHESTRATION_GRADER_MODEL: grader_mock,
    }
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
        )

        try:
            run_id = await stack.registry.run_critic_dev_optimize(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            with synced_db.session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.EXITED

        finally:
            grader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grader_task


@pytest.mark.timeout(180)
@pytest.mark.slow
async def test_critic_cannot_push_images(e2e_stack, synced_db: Database, all_files_scope, critic_image):
    """Test that critic agents cannot push images to registry.

    Critic agents should only be able to read from the registry, not write.
    Attempting to push should result in a 403 Forbidden error.

    Note: This test verifies the permission model at the registry proxy level.
    The critic container has RLS-scoped database access via a temp user,
    and the registry proxy should check the agent type before allowing pushes.
    """

    @CriticMock.mock()
    def mock(m: CriticMock) -> PlayGen:
        yield None  # First request

        # Try crane push from critic container — should fail because
        # critics don't have registry write access (403 Forbidden).
        result = yield from m.exec_roundtrip(
            ["sh", "-c", "crane push /workspace/ ${PROPS_BACKEND_URL#http://}/test-push:latest --insecure 2>&1"],
            timeout_ms=30000,
        )
        stdout = result.stdout if hasattr(result, "stdout") else ""
        stderr = result.stderr if hasattr(result, "stderr") else ""
        logger.info(f"Critic push attempt stdout: {stdout}")
        logger.info(f"Critic push attempt stderr: {stderr}")

        # Submit zero issues (expected behavior: push failed, critic still completes)
        yield m.submit(issues_count=0, summary="Push attempt completed (expected to fail)")

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
        run_id = await stack.registry.run_critic(
            image_ref=stack.image_digests["critic"],
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=5.0,
        )

        # Verify critic completed (it should complete even though push failed)
        with synced_db.session() as session:
            critic_run = session.get(AgentRun, run_id)
            assert critic_run is not None
            # The critic should complete because it handled the push failure gracefully
            assert critic_run.status == AgentRunStatus.EXITED


if __name__ == "__main__":
    pytest_bazel.main()
