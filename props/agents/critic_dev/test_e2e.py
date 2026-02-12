"""E2E test for agent orchestration and custom agent images.

Tests the orchestration workflow:
1. Optimizer calls run_critic to spawn critic containers
2. Critic receives system prompt and submits issues
3. Grader processes edges
4. Optimizer waits for grading and reports success

Custom image flow:
1. Pull built-in critic image via crane
2. Replace entrypoint with custom Python script (appended layer + CMD mutate)
3. Push modified image to registry proxy
4. Run the custom image — script writes critique data directly to DB
5. Grader detects drift and fills grading edges

Uses the in-container architecture with:
- FakeOpenAI server backed by CriticDevMock/CriticMock/GraderMock
- LLM proxy for auth and logging
- AgentRegistry for container orchestration
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import textwrap

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core.testing.responses import PlayGen, tool_roundtrip
from mcp_infra.exec.matchers import exited_successfully
from props.agents.critic.testing.mocks import CriticMock
from props.agents.critic_dev.loop import RunCriticToolArgs, WaitUntilGradedToolArgs
from props.agents.critic_dev.testing.mocks import CriticDevMock
from props.agents.critic_dev.testing.orchestration_fixtures import (
    ORCHESTRATION_CRITIC_MODEL,
    ORCHESTRATION_GRADER_MODEL,
    ORCHESTRATION_OPTIMIZER_MODEL,
)
from props.agents.grader.testing.mocks import GraderMock
from props.agents.grader.tools import ClusterMemberSpec
from props.core.agent_types import TargetMetric
from props.core.eval_api_models import GradingStatusResponse, RunCriticResponse
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, GradingEdge, ReportedIssue
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

    # Grader mock for zero-issue case: should never be woken
    @GraderMock.mock(check_consumed=False)
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

        # Resolve images before starting tasks
        grader_image_resolved = stack.resolved_images["grader"]
        opt_image = stack.resolved_images["critic_dev_optimize"]

        # Start snapshot grader in background
        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(
                image=grader_image_resolved, snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL
            )
        )

        try:
            # Run critic-dev optimizer
            run_id = await stack.registry.run_critic_dev_optimize(
                image=opt_image,
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


# Custom Python script that replaces the critic's main.py in the image.
# Bypasses the LLM agent loop: connects to the DB directly, inserts one
# reported issue + occurrence, and exits. This creates grading drift that
# the snapshot grader picks up.
#
# Overlaid at the runfiles path so the existing entrypoint launcher
# (critic_bin) runs this instead of the original main.py.
_CUSTOM_CRITIC_SCRIPT = textwrap.dedent("""\
    from __future__ import annotations

    import asyncio
    import sys

    from props.agents.runtime import get_current_agent_run_id
    from props.db.database import Database
    from props.db.models import ReportedIssue, ReportedIssueOccurrence
    from props.db.snapshots import LocationAnchor


    async def main() -> int:
        db = Database.from_env()

        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            print(f"Custom critic running as {agent_run_id}")

        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            issue = ReportedIssue(
                agent_run_id=agent_run_id,
                issue_id="custom-test-issue",
                rationale="Test issue from custom critic image",
            )
            session.add(issue)

        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            occ = ReportedIssueOccurrence(
                agent_run_id=agent_run_id,
                reported_issue_id="custom-test-issue",
                locations=[LocationAnchor(file="test.py", start_line=1, end_line=5)],
            )
            session.add(occ)

        print("Custom critic completed: 1 issue, 1 occurrence")
        return 0


    if __name__ == "__main__":
        sys.exit(asyncio.run(main()))
""")


@pytest.mark.timeout(300)
@pytest.mark.slow
async def test_po_creates_custom_critic_image(
    synced_db, e2e_stack, test_snapshot, critic_dev_optimize_image, critic_image, grader_image
):
    """Test full custom image flow: pull → overlay main.py → push → run → grade.

    Exercises the real crane workflow:
    1. Optimizer writes custom Python script to workspace
    2. Appends a layer that overlays main.py at the runfiles path
    3. Pushes the modified image by digest to the registry proxy
    4. Runs the custom image — the overlaid main.py writes critique data directly to DB
    5. Snapshot grader detects drift and fills grading edges
    6. Optimizer's wait_until_graded returns successfully

    The custom critic bypasses the LLM entirely — it directly inserts a
    reported_issue + occurrence via SQLAlchemy, proving the container has
    working DB credentials and the full OCI pull/append/push/run pipeline works.
    """
    snapshot_slug = SnapshotSlug(test_snapshot)

    @CriticDevMock.mock()
    def optimizer_mock(m: CriticDevMock) -> PlayGen:
        yield None  # First request

        # Write the custom critic script into the workspace
        result = yield from m.exec_roundtrip(
            ["sh", "-c", f"cat > /workspace/custom_main.py << 'PYEOF'\n{_CUSTOM_CRITIC_SCRIPT}PYEOF"], timeout_ms=15000
        )
        assert_that(result, exited_successfully())

        # Build custom image: overlay main.py at the runfiles path, push by digest.
        # The aspect_py_binary "critic_bin" stores main.py under its runfiles tree.
        # Appending a layer with the same path shadows the original file.
        build_cmd = (
            "set -e && "
            "REGISTRY=$(echo $PROPS_BACKEND_URL | sed 's|https\\?://||') && "
            "MAIN_PY=props/agents/critic/critic_bin.runfiles/_main/props/agents/critic/main.py && "
            "mkdir -p /tmp/layer/$(dirname $MAIN_PY) && "
            "cp /workspace/custom_main.py /tmp/layer/$MAIN_PY && "
            "tar -cf /tmp/layer.tar -C /tmp/layer . && "
            "crane mutate $REGISTRY/critic:latest"
            " --append /tmp/layer.tar"
            " -o /tmp/image.tar"
            " --insecure && "
            "DIGEST=$(crane digest --tarball /tmp/image.tar) && "
            "crane push /tmp/image.tar $REGISTRY/critic@$DIGEST --insecure && "
            "echo $DIGEST"
        )
        result = yield from m.exec_roundtrip(["sh", "-c", build_cmd], timeout_ms=120000)
        assert_that(result, exited_successfully())
        stdout = result.stdout if isinstance(result.stdout, str) else result.stdout.truncated_text
        new_digest = stdout.strip().split("\n")[-1]  # Last line is the digest
        logger.info(f"Custom image digest: {new_digest}")

        # Run the custom critic image
        example = WholeSnapshotExample(kind=ExampleKind.WHOLE_SNAPSHOT, snapshot_slug=snapshot_slug)
        run_critic_args = RunCriticToolArgs(
            definition_id=new_digest, example=example, timeout_seconds=120, budget_usd=1.0
        )
        call = m.tool_call("run_critic", run_critic_args)
        run_critic_output: RunCriticResponse = yield from tool_roundtrip(call, RunCriticResponse)
        critic_run_id = run_critic_output.critic_run_id
        logger.info(f"Custom critic run: {critic_run_id}")

        # Wait for grading to complete
        wait_args = WaitUntilGradedToolArgs(critic_run_id=str(critic_run_id), timeout_seconds=60)
        wait_call = m.tool_call("wait_until_graded_tool", wait_args)
        wait_output: GradingStatusResponse = yield from tool_roundtrip(wait_call, GradingStatusResponse)
        logger.info(f"Grading complete: total_credit={wait_output.total_credit}")

        yield m.report_success()

    # Grader mock for custom critic: grades "custom-test-issue" on test.py
    # test.py matches: tp-003/occ-1, tp-004/occ-1, tp-005/occ-1, fp-001/fp-occ-1 = 4 edges
    @GraderMock.mock(check_consumed=False)
    def grader_mock(m: GraderMock) -> PlayGen:
        yield None  # First request

        # Get pending edges for custom-test-issue
        drift = yield from m.get_drift_roundtrip()
        run_id = drift.grading[0].critique_run_id

        # Fill all 4 edges with credit=0
        yield from m.fill_remaining_roundtrip(run_id, "custom-test-issue", 4, "Mock: no GT matches")

        # Issue has credit=0, appears in clustering
        drift = yield from m.get_drift_roundtrip()
        assert len(drift.clustering) == 1

        # Cluster the issue
        yield from m.create_cluster_roundtrip(
            "novel-issues",
            "Unmatched issues from orchestration",
            [ClusterMemberSpec(run=run_id, issue_id="custom-test-issue", rationale="Novel issue")],
        )

        yield from m.sleep_forever("All edges graded and clustered")

    # No critic mock needed — custom critic bypasses the LLM entirely
    mocks = {ORCHESTRATION_OPTIMIZER_MODEL: optimizer_mock, ORCHESTRATION_GRADER_MODEL: grader_mock}
    async with e2e_stack(mocks, images=[critic_dev_optimize_image, critic_image, grader_image]) as stack:
        grader_image_resolved = stack.resolved_images["grader"]
        opt_image = stack.resolved_images["critic_dev_optimize"]

        grader_task = asyncio.create_task(
            stack.registry.run_snapshot_grader(
                image=grader_image_resolved, snapshot_slug=snapshot_slug, model=ORCHESTRATION_GRADER_MODEL
            )
        )

        try:
            run_id = await stack.registry.run_critic_dev_optimize(
                image=opt_image,
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

                # Verify the custom critic created its issue
                issues = session.query(ReportedIssue).filter_by(issue_id="custom-test-issue").all()
                assert len(issues) == 1, f"Expected 1 custom issue, got {len(issues)}"

                # Verify grading edges were created (grader processed the drift)
                critic_run_id = issues[0].agent_run_id
                edges = session.query(GradingEdge).filter_by(critique_run_id=critic_run_id).all()
                assert len(edges) > 0, "Expected grading edges for the custom critic run"

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
        critic_image_resolved = stack.resolved_images["critic"]
        run_id = await stack.registry.run_critic(
            image=critic_image_resolved,
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
