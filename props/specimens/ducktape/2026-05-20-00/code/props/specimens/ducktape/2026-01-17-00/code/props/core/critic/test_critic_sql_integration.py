"""Integration test for critic SQL workflow.

Tests the end-to-end SQL workflow where critic agents:
1. Get temporary database credentials with RLS scoping
2. Write reported issues directly to PostgreSQL (using locations JSONB array)
3. Call critic_submit tool to finalize the critique
4. Validation occurs on submit (not on every INSERT)
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from props.core.critic.conftest import insert_issue, insert_occurrence
from props.core.critic.submit_server import CriticSubmitInput
from props.core.db.examples import Example
from props.core.db.models import AgentRun, AgentRunStatus, ReportedIssue, ReportedIssueOccurrence
from props.core.db.session import get_session
from props.core.db.snapshots import DBLocationAnchor
from props.core.ids import SnapshotSlug
from props.testing.fixtures import make_critic_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


async def test_critic_sql_basic_workflow(test_critic_run, temp_engine, submit_server):
    """Test basic critic SQL workflow with single-location occurrence."""
    # Simulate agent actions using temp user credentials
    with temp_engine.connect() as conn:
        # Agent inserts reported issue
        insert_issue(conn, "dead-code-utils", "Function cleanup() is never called")

        # Agent inserts occurrence with locations (uses actual fixture file)
        insert_occurrence(conn, "dead-code-utils", [DBLocationAnchor(file="add.py", start_line=1, end_line=3)])
        conn.commit()

    # Agent calls submit tool

    tool_result = await submit_server.submit_tool.run(
        CriticSubmitInput(issues_count=1, summary="Found 1 dead code issue").model_dump()
    )

    # Verify result
    result = tool_result.structured_content
    assert result["message"] == "Review completed successfully with 1 issues"
    assert result["issues_count"] == 1
    assert result["occurrences_count"] == 1

    # Verify database state
    with get_session() as session:
        critic_run = session.get(AgentRun, test_critic_run)
        assert critic_run is not None
        assert critic_run.status == AgentRunStatus.COMPLETED
        assert critic_run.completion_summary == "Found 1 dead code issue"

        # Verify reported issue exists
        issue = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run, issue_id="dead-code-utils").one()
        assert issue.rationale == "Function cleanup() is never called"

        # Verify occurrence exists with locations
        occ = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="dead-code-utils")
            .one()
        )
        assert occ.locations == [DBLocationAnchor(file="add.py", start_line=1, end_line=3)]


async def test_critic_sql_multi_location_occurrence(test_critic_run, temp_engine, submit_server):
    """Test critic SQL workflow with multi-location occurrence (e.g., duplicated code)."""
    with temp_engine.connect() as conn:
        # Insert issue with multi-location occurrence
        insert_issue(conn, "duplicated-enum", "Status enum duplicated across files")

        # Multi-location occurrence (duplicated code in two fixture files)
        insert_occurrence(
            conn,
            "duplicated-enum",
            [
                DBLocationAnchor(file="add.py", start_line=1, end_line=3),
                DBLocationAnchor(file="subtract.py", start_line=1, end_line=3),
            ],
        )
        conn.commit()

    # Submit

    tool_result = await submit_server.submit_tool.run(
        CriticSubmitInput(issues_count=1, summary="Found 1 duplication issue").model_dump()
    )

    result = tool_result.structured_content
    assert result["issues_count"] == 1
    assert result["occurrences_count"] == 1

    # Verify multi-location occurrence
    with get_session() as session:
        occ = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="duplicated-enum")
            .one()
        )
        assert occ.locations == [
            DBLocationAnchor(file="add.py", start_line=1, end_line=3),
            DBLocationAnchor(file="subtract.py", start_line=1, end_line=3),
        ]


async def test_critic_sql_rls_isolation(test_critic_run, test_snapshot, temp_engine):
    """Test RLS isolation - agents can only see their own run's data."""

    # Create another critic run (different agent) using a different example
    other_run_id = None
    with get_session() as session:
        # Get a different example (any one from test fixtures will do)
        other_example = (
            session.query(Example)
            .filter_by(snapshot_slug=SnapshotSlug("test-fixtures/train1"))
            .filter(Example.files_hash.isnot(None))  # Get a file_set example
            .first()
        )
        assert other_example is not None, "Need another example for RLS test"

        other_run = make_critic_run(example=other_example, status=AgentRunStatus.IN_PROGRESS)
        session.add(other_run)
        session.commit()
        other_run_id = other_run.agent_run_id

    # Insert data from other run (using admin credentials)
    with get_session() as session:
        issue = ReportedIssue(agent_run_id=other_run_id, issue_id="other-issue", rationale="Other agent's issue")
        session.add(issue)
        session.commit()

    # Now agent with temp_creds inserts their own data
    with temp_engine.connect() as conn:
        insert_issue(conn, "my-issue", "My issue")
        conn.commit()

        # Query should only see own run's data
        result = conn.execute(text("SELECT issue_id FROM reported_issues ORDER BY issue_id"))
        issue_ids = [row[0] for row in result]

        # Should ONLY see "my-issue", not "other-issue"
        assert issue_ids == ["my-issue"]


async def test_critic_sql_validation_on_submit(test_critic_run, temp_engine, submit_server):
    """Test validation occurs on submit (invalid file path caught)."""

    with temp_engine.connect() as conn:
        # Insert issue with INVALID file path (doesn't exist)
        insert_issue(conn, "bad-issue", "Issue in non-existent file")

        insert_occurrence(
            conn,
            "bad-issue",
            [DBLocationAnchor(file="nonexistent.py")],  # File doesn't exist!
        )
        conn.commit()

    # Submit should FAIL with validation error

    with pytest.raises(ToolError, match="does not exist in snapshot"):
        await submit_server.submit_tool.run(CriticSubmitInput(issues_count=1, summary="Found 1 issue").model_dump())

    # Verify run status NOT changed to completed
    with get_session() as session:
        critic_run = session.get(AgentRun, test_critic_run)
        assert critic_run is not None
        assert critic_run.status != AgentRunStatus.COMPLETED  # Should not be marked completed due to validation failure


async def test_critic_sql_issue_without_occurrence_fails(test_critic_run, temp_engine, submit_server):
    """Test that submitting an issue without occurrences fails validation.

    Every reported issue must have at least one occurrence showing where it occurs in the code.
    """
    with temp_engine.connect() as conn:
        # Insert issue WITHOUT any occurrences
        insert_issue(conn, "dead-code", "Unused import")
        conn.commit()

    # Submit should FAIL with validation error
    with pytest.raises(ToolError, match="Issue 'dead-code' has no occurrences"):
        await submit_server.submit_tool.run(CriticSubmitInput(issues_count=1, summary="Found 1 issue").model_dump())

    # Verify run status NOT changed to completed
    with get_session() as session:
        critic_run = session.get(AgentRun, test_critic_run)
        assert critic_run is not None
        assert critic_run.status != AgentRunStatus.COMPLETED

        # Verify issue was created but has no occurrences
        issue = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run, issue_id="dead-code").first()
        assert issue is not None
        assert issue.rationale == "Unused import"

        occurrences = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="dead-code")
            .all()
        )
        assert len(occurrences) == 0


async def test_critic_sql_multiple_issues_and_occurrences(test_critic_run, temp_engine, submit_server):
    """Test workflow with multiple issues and multiple occurrences per issue."""
    with temp_engine.connect() as conn:
        # Issue 1: Dead code with 2 occurrences (in fixture files)
        insert_issue(conn, "dead-code", "Multiple unused functions")

        insert_occurrence(conn, "dead-code", [DBLocationAnchor(file="add.py", start_line=1, end_line=3)])

        insert_occurrence(conn, "dead-code", [DBLocationAnchor(file="subtract.py", start_line=1, end_line=3)])

        # Issue 2: Type error with 1 occurrence
        insert_issue(conn, "type-error", "Missing type annotation")

        insert_occurrence(conn, "type-error", [DBLocationAnchor(file="multiply.py", start_line=1)])

        conn.commit()

    # Submit

    tool_result = await submit_server.submit_tool.run(
        CriticSubmitInput(issues_count=2, summary="Found 2 issues with 3 total occurrences").model_dump()
    )

    result = tool_result.structured_content
    assert result["issues_count"] == 2
    assert result["occurrences_count"] == 3

    # Verify all issues and occurrences
    with get_session() as session:
        issues = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run).all()
        assert len(issues) == 2

        # Check occurrences for dead-code
        dead_occs = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="dead-code")
            .all()
        )
        assert len(dead_occs) == 2

        # Check occurrence for type-error
        type_occs = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="type-error")
            .all()
        )
        assert len(type_occs) == 1


async def test_insert_issue(test_critic_run, temp_engine):
    """Test insert_issue helper."""

    with temp_engine.connect() as conn:
        insert_issue(conn, "test-issue", "Test rationale")
        conn.commit()

    with get_session() as session:
        issue = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run, issue_id="test-issue").one()
        assert issue.rationale == "Test rationale"
