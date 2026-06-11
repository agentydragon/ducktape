"""E2E tests for critic agent with in-container agent loop.

Tests the critic agent end-to-end using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from PropsMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → PropsMock

Covers:
- Zero issues submission (clean code)
- Issue submission workflow
"""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import assert_that

from agent_core_testing.responses import PlayGen
from mcp_infra.exec.matchers import exited_successfully
from props.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.db.models import AgentRun, AgentRunStatus
from props.db.session import get_session
from props.testing.mocks import PropsMock

# Test timeout (seconds) - applies to container execution
TEST_TIMEOUT_SECONDS = 120


def make_critic_mock_zero_issues() -> PropsMock:
    """Create mock for critic that finds zero issues."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Reviewed code, no issues found"])

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_zero_issues(e2e_stack, test_snapshot, all_files_scope):
    """Test critic successfully submits zero issues."""
    mock = make_critic_mock_zero_issues()

    async with e2e_stack(mock) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=CRITIC_IMAGE_REF,
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=None,
        )

        assert critic_run_id is not None

        # Verify database records
        with get_session() as session:
            run = session.get(AgentRun, critic_run_id)
            assert run is not None
            assert run.critic_config().example.snapshot_slug == test_snapshot
            assert run.status == AgentRunStatus.COMPLETED
            assert len(run.reported_issues) == 0


def make_critic_mock_with_issues() -> PropsMock:
    """Create mock for critic that finds and submits issues."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-issue", "dead-import", "Unused import detected in subtract.py"]
        )
        assert_that(result, exited_successfully())
        result = yield from m.docker_exec_roundtrip(
            ["critique", "insert-occurrence", "dead-import", "subtract.py", "-s", "1", "-e", "1"]
        )
        assert_that(result, exited_successfully())
        yield from m.docker_exec_roundtrip(["critique", "submit", "1", "Found 1 dead code issue"])

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_submit_with_issues(e2e_stack, test_snapshot, all_files_scope):
    """Test critic submits an issue with occurrence."""
    mock = make_critic_mock_with_issues()

    async with e2e_stack(mock) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=CRITIC_IMAGE_REF,
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=None,
        )

        assert critic_run_id is not None

        # Verify database records
        with get_session() as session:
            run = session.get(AgentRun, critic_run_id)
            assert run is not None
            assert run.critic_config().example.snapshot_slug == test_snapshot
            assert run.status == AgentRunStatus.COMPLETED

            # Check that the issue was actually stored
            assert len(run.reported_issues) == 1
            issue = run.reported_issues[0]
            assert issue.issue_id == "dead-import"
            assert "Unused import" in issue.rationale

            # Check occurrence
            assert len(issue.occurrences) == 1
            occurrence = issue.occurrences[0]
            assert len(occurrence.locations) == 1
            assert occurrence.locations[0].file == "subtract.py"


if __name__ == "__main__":
    pytest_bazel.main()
