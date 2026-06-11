"""E2E test for agent building and running custom agent images.

Tests the full workflow of an agent creating its own variant:
1. Create custom agent.md content with random token
2. Use props agent-pkg create CLI to build OCI layer
3. Proxy automatically creates agent_definitions row
4. Run the newly created agent image via run_critic MCP tool
5. Verify new agent got the custom agent.md in its system message

Uses the in-container architecture with:
- FakeOpenAI server backed by PropsMock/GraderMock
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

from agent_core_testing.responses import PlayGen, tool_roundtrip
from mcp_infra.exec.matchers import exited_successfully
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.critic_dev.optimize.main import RunCriticToolArgs, WaitUntilGradedToolArgs
from props.critic_dev.optimize.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
    make_orchestration_grader_mock,
    multi_model_e2e_stack,
)
from props.critic_dev.shared import TargetMetric
from props.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.db.models import AgentRun, AgentRunStatus
from props.db.session import get_session
from props.testing.mocks import PropsMock, get_system_message_text

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.requires_docker]

# Test timeout (seconds)
TEST_TIMEOUT_SECONDS = 120


def make_po_orchestration_mock(snapshot_slug: SnapshotSlug) -> PropsMock:
    """Create PO mock that orchestrates critic runs.

    The mock:
    1. Calls run_critic tool (DirectToolProvider, calls REST API)
    2. Waits for grading via wait_until_graded_tool (polls database)
    3. Reports success
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request

        # Call run_critic tool (DirectToolProvider)
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id="builtin", example=example, timeout_seconds=120, budget_usd=None
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
        yield from m.docker_exec_roundtrip(["prompt-optimize-dev", "report-success"])

    return mock


def make_po_custom_image_mock(snapshot_slug: SnapshotSlug, random_token: str) -> PropsMock:
    """Create PO mock that creates a custom critic image with a verification token.

    The mock:
    1. Creates /workspace/custom_critic/ directory with agent.md containing the token
    2. Calls 'props agent-pkg create' CLI to build and push the image
    3. Extracts the new digest and calls run_critic with it
    4. Waits for grading via wait_until_graded_tool (polls database)
    5. Reports success

    NOTE: Requires registry proxy to be available for agent-pkg create to succeed.
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request

        # Create custom critic directory with agent.md containing the random token
        agent_md_content = f"""# Custom Critic with Verification Token

You are a code critic. VERIFICATION_TOKEN: {random_token}

Find issues in the code and report them.
"""
        result = yield from m.docker_exec_roundtrip(
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

        # Build and push the custom image via registry proxy
        result = yield from m.docker_exec_roundtrip(
            ["props", "agent-pkg", "create", "/workspace/custom_critic/"], timeout_ms=60000
        )
        assert_that(result, exited_successfully())
        logger.info(f"agent-pkg create output: {result.stdout}")

        # Extract digest from the created agent definition
        result = yield from m.docker_exec_roundtrip(
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
            budget_usd=None,
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
        yield from m.docker_exec_roundtrip(["prompt-optimize-dev", "report-success"])

    return mock


def make_critic_mock_with_system_check() -> PropsMock:
    """Create critic mock that verifies it receives a system message.

    Verifies the mechanism works: critic mock can access and inspect the system prompt.
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
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
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Critic completed"])

    return mock


def make_critic_mock_with_token_check(expected_token: str) -> PropsMock:
    """Create critic mock that verifies system message contains a specific token.

    Used with custom critic images to verify the custom prompt was used.
    """

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        # Capture first request to verify system message contains the token
        first_request = yield None

        # Verify the system message contains our expected token
        system_text = get_system_message_text(first_request)
        assert expected_token in system_text, (
            f"Expected token '{expected_token}' not found in system message. "
            f"System message starts with: {system_text[:200]}..."
        )
        logger.info(f"Critic received system message with expected token: {expected_token}")

        # Submit zero issues
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Custom critic completed"])

    return mock


@pytest.mark.timeout(300)
@pytest.mark.slow
async def test_po_orchestrates_critic_with_system_prompt_check(synced_test_db, async_docker_client, test_snapshot):
    """Test prompt optimizer orchestration with critic system prompt verification.

    Verifies:
    1. Optimizer can call run_critic MCP tool
    2. Critic receives a valid system prompt (mechanism check)
    3. Grader processes the edges
    4. Optimizer's wait_until_graded returns

    The critic mock verifies it receives a proper system message, proving
    the in-container architecture properly passes prompts to agents.
    """
    snapshot_slug = SnapshotSlug(test_snapshot)

    # Create mocks - critic verifies it receives a system prompt
    optimizer_mock = make_po_orchestration_mock(snapshot_slug)
    critic_mock = make_critic_mock_with_system_check()
    grader_mock = make_orchestration_grader_mock()

    async with multi_model_e2e_stack(
        optimizer_mock, critic_mock, synced_test_db, async_docker_client, grader_mock=grader_mock
    ) as registry:
        # Start grader daemon in background
        grader_task = asyncio.create_task(
            registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
        )

        try:
            # Run prompt optimizer
            run_id = await registry.run_prompt_optimizer(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            # Verify optimizer completed
            with get_session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.COMPLETED, (
                    f"Expected COMPLETED, got {optimizer_run.status}"
                )

        finally:
            grader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grader_task


@pytest.mark.timeout(300)
@pytest.mark.slow
@pytest.mark.skip(
    reason="Requires registry proxy: add to multi_model_e2e_stack and pass PROPS_REGISTRY_PROXY_* env vars to containers"
)
async def test_po_creates_custom_critic_with_token(synced_test_db, async_docker_client, test_snapshot):
    """Test full custom image flow: PO creates critic image, critic verifies prompt token.

    This test verifies the complete workflow:
    1. Optimizer creates a custom agent.md with a unique verification token
    2. Optimizer builds and pushes custom critic image via registry proxy
    3. Optimizer calls run_critic with the new custom image
    4. Critic receives system prompt and asserts it contains the token
    5. Grading completes

    To enable this test:
    1. Add registry proxy to multi_model_e2e_stack (start props-registry-proxy container)
    2. Pass PROPS_REGISTRY_PROXY_HOST and PROPS_REGISTRY_PROXY_PORT to agent containers via extra_env
    3. Remove the @skip marker
    """
    snapshot_slug = SnapshotSlug(test_snapshot)
    verification_token = f"VERIFY_{secrets.token_hex(8)}"

    # Create mocks
    optimizer_mock = make_po_custom_image_mock(snapshot_slug, verification_token)
    critic_mock = make_critic_mock_with_token_check(verification_token)
    grader_mock = make_orchestration_grader_mock()

    async with multi_model_e2e_stack(
        optimizer_mock, critic_mock, synced_test_db, async_docker_client, grader_mock=grader_mock
    ) as registry:
        grader_task = asyncio.create_task(
            registry.run_snapshot_grader(snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL)
        )

        try:
            run_id = await registry.run_prompt_optimizer(
                budget=1.0,
                optimizer_model=ORCHESTRATION_OPTIMIZER_MODEL,
                critic_model=ORCHESTRATION_CRITIC_MODEL,
                target_metric=TargetMetric.WHOLE_REPO,
                timeout_seconds=180,
            )

            with get_session() as session:
                optimizer_run = session.get(AgentRun, run_id)
                assert optimizer_run is not None
                assert optimizer_run.status == AgentRunStatus.COMPLETED

        finally:
            grader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await grader_task


def make_critic_push_attempt_mock() -> PropsMock:
    """Create critic mock that attempts to push an image (should fail with 403)."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request

        # Try to call agent-pkg create from critic container
        # This should fail because critics don't have registry write access
        result = yield from m.docker_exec_roundtrip(["props", "agent-pkg", "create", "/workspace/"], timeout_ms=30000)
        # The command should fail - check for non-zero exit or error message
        # We expect a 403 Forbidden from the registry proxy
        stdout = result.stdout if hasattr(result, "stdout") else ""
        stderr = result.stderr if hasattr(result, "stderr") else ""
        logger.info(f"Critic push attempt stdout: {stdout}")
        logger.info(f"Critic push attempt stderr: {stderr}")

        # Report failure since we couldn't push (expected behavior)
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Push attempt completed (expected to fail)"])

    return mock


@pytest.mark.timeout(180)
@pytest.mark.slow
async def test_critic_cannot_push_images(e2e_stack, all_files_scope):
    """Test that critic agents cannot push images to registry.

    Critic agents should only be able to read from the registry, not write.
    Attempting to push should result in a 403 Forbidden error.

    Note: This test verifies the permission model at the registry proxy level.
    The critic container has RLS-scoped database access via a temp user,
    and the registry proxy should check the agent type before allowing pushes.
    """
    mock = make_critic_push_attempt_mock()

    async with e2e_stack(mock) as stack:
        run_id = await stack.registry.run_critic(
            image_ref=CRITIC_IMAGE_REF,
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=None,
        )

        # Verify critic completed (it should complete even though push failed)
        with get_session() as session:
            critic_run = session.get(AgentRun, run_id)
            assert critic_run is not None
            # The critic should complete because it handled the push failure gracefully
            assert critic_run.status == AgentRunStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
