"""E2E tests for critic agent with HTTP MCP mode.

Tests the critic agent end-to-end using:
- Real Docker containers
- Real PostgreSQL database with temporary RLS-scoped users
- Mocked OpenAI responses
- HTTP MCP transport with bearer token auth

Covers:
- Zero issues submission (clean code)
- Issue submission workflow
- Infinite loop prevention (regression test)
"""

from __future__ import annotations

import pytest
from hamcrest import assert_that

from agent_core.events import ApiRequest, SystemText, ToolCall
from agent_core_testing.responses import PlayGen
from agent_core_testing.steps import exited_successfully
from props.core.db.agent_definition_ids import CRITIC_IMAGE_REF
from props.core.db.models import AgentRun, AgentRunStatus, Event
from props.core.db.session import get_session
from props.testing.mocks import PropsMock


def make_critic_mock_zero_issues() -> PropsMock:
    """Create mock for critic that finds zero issues."""

    @PropsMock.mock()
    def mock(m: PropsMock) -> PlayGen:
        yield None  # First request
        yield from m.docker_exec_roundtrip(["critique", "submit", "0", "Reviewed code, no issues found"])

    return mock


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_http_mode_zero_issues(test_registry, test_snapshot, all_files_scope):
    """Test critic successfully submits zero issues using HTTP MCP mode."""
    mock = make_critic_mock_zero_issues()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
    )

    assert critic_run_id is not None

    # Verify database records
    with get_session() as session:
        run = session.get(AgentRun, critic_run_id)
        assert run is not None
        assert run.critic_config().example.snapshot_slug == test_snapshot
        assert run.status == AgentRunStatus.COMPLETED
        assert len(run.reported_issues) == 0

        # Verify events were persisted
        events = session.query(Event).filter_by(agent_run_id=critic_run_id).order_by(Event.sequence_num).all()
        assert len(events) > 0, "Expected at least one event to be persisted"

        api_request_events = [e for e in events if isinstance(e.payload, ApiRequest)]
        assert len(api_request_events) >= 1, "Expected at least one api_request event"

        tool_call_events = [e for e in events if isinstance(e.payload, ToolCall)]
        assert len(tool_call_events) >= 1, "Expected at least one tool_call event"

        system_text_events = [e for e in events if isinstance(e.payload, SystemText)]
        assert len(system_text_events) == 1, "Expected exactly one system_text event"
        assert system_text_events[0].sequence_num == 0, "System text should be first event (sequence 0)"


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_does_not_infinite_loop_on_zero_issues(test_registry, all_files_scope):
    """Verify critic doesn't get stuck in infinite loop when finding zero issues."""
    mock = make_critic_mock_zero_issues()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
    )

    assert critic_run_id is not None
    assert mock.consumed, "Mock should be fully consumed"


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
async def test_critic_http_mode_submit_with_issues(test_registry, test_snapshot, all_files_scope):
    """Test critic HTTP mode with actual issue submission."""
    mock = make_critic_mock_with_issues()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
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


# =============================================================================
# AgentHandle-based flow tests (run_critic)
# =============================================================================


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
async def test_critic_zero_issues(test_registry, test_snapshot, all_files_scope):
    """Test run_critic with AgentHandle-based flow."""
    mock = make_critic_mock_zero_issues()
    critic_run_id = await test_registry.run_critic(
        image_ref=CRITIC_IMAGE_REF, example=all_files_scope, client=mock, max_turns=100
    )

    assert critic_run_id is not None

    # Verify database records
    with get_session() as session:
        run = session.get(AgentRun, critic_run_id)
        assert run is not None
        assert run.critic_config().example.snapshot_slug == test_snapshot
        assert run.status == AgentRunStatus.COMPLETED
        assert len(run.reported_issues) == 0
