"""E2E tests for critic agent with in-container agent loop.

Tests the critic agent end-to-end using:
- Real Docker containers running agent loops
- Real PostgreSQL database with temporary RLS-scoped users
- Real LLM proxy (validates auth, logs requests)
- Fake OpenAI server (returns scripted responses from DecoratorMock)

The test stack is:
    Container → LLM Proxy → Fake OpenAI → DecoratorMock

Covers:
- Zero issues submission (clean code)
- Issue submission workflow
"""

from __future__ import annotations

import pytest
import pytest_bazel

from agent_core.testing.responses import DecoratorMock, PlayGen
from props.agents.critic.main import InsertIssueArgs, InsertOccurrenceArgs, SubmitArgs
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.testing.constants import DEFAULT_TEST_MODEL

# Test timeout (seconds) - applies to container execution
TEST_TIMEOUT_SECONDS = 120


def make_critic_mock_zero_issues() -> DecoratorMock:
    """Create mock for critic that finds zero issues."""

    @DecoratorMock.mock(check_consumed=False)
    def mock(m: DecoratorMock) -> PlayGen:
        yield None  # First request
        yield m.tool_call("submit", SubmitArgs(issues_count=0, summary="Reviewed code, no issues found"))

    return mock


@pytest.mark.requires_docker
async def test_critic_zero_issues(e2e_stack, test_snapshot, all_files_scope, critic_image, db: Database):
    """Test critic successfully submits zero issues."""
    mock = make_critic_mock_zero_issues()

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=stack.image_digests["critic"],
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=5.0,
        )

        assert critic_run_id is not None

        # Verify database records
        with db.session() as session:
            run = session.get(AgentRun, critic_run_id)
            assert run is not None
            assert run.critic_config().example.snapshot_slug == test_snapshot
            assert run.status == AgentRunStatus.EXITED
            assert len(run.reported_issues) == 0


def make_critic_mock_with_issues() -> DecoratorMock:
    """Create mock for critic that finds and submits issues."""

    @DecoratorMock.mock(check_consumed=False)
    def mock(m: DecoratorMock) -> PlayGen:
        yield None  # First request
        yield m.tool_call(
            "insert_issue", InsertIssueArgs(issue_id="dead-import", rationale="Unused import detected in subtract.py")
        )
        yield m.tool_call(
            "insert_occurrence",
            InsertOccurrenceArgs(issue_id="dead-import", file="subtract.py", start_line=1, end_line=1),
        )
        yield m.tool_call("submit", SubmitArgs(issues_count=1, summary="Found 1 dead code issue"))

    return mock


@pytest.mark.requires_docker
async def test_critic_submit_with_issues(e2e_stack, test_snapshot, all_files_scope, critic_image, db: Database):
    """Test critic submits an issue with occurrence."""
    mock = make_critic_mock_with_issues()

    async with e2e_stack({DEFAULT_TEST_MODEL: mock}, images=[critic_image]) as stack:
        critic_run_id = await stack.registry.run_critic(
            image_ref=stack.image_digests["critic"],
            example=all_files_scope,
            model=stack.model,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            parent_run_id=None,
            budget_usd=5.0,
        )

        assert critic_run_id is not None

        # Verify database records
        with db.session() as session:
            run = session.get(AgentRun, critic_run_id)
            assert run is not None
            assert run.critic_config().example.snapshot_slug == test_snapshot
            assert run.status == AgentRunStatus.EXITED

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
