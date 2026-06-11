"""Integration test for grader SQL workflow.

Tests the end-to-end SQL workflow where grader agents:
1. Get temporary database credentials with RLS scoping
2. Write grading edges directly to PostgreSQL
3. Call grader_submit tool to finalize grading
4. Validation occurs on submit (ensures all edges are created)
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine, text

from props.core.db.models import AgentRun, AgentRunStatus, GradingEdge, ReportedIssue
from props.core.db.session import get_session
from props.core.db.temp_user_manager import TempUserManager
from props.core.grader.conftest import make_test_grader_run
from props.core.grader.submit_server import GraderSubmitServer

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
async def grader_temp_creds(test_db, test_grader_run):
    """Create temporary database user with RLS scoping."""
    async with TempUserManager(test_db.admin, test_grader_run) as creds:
        yield creds


@pytest.fixture
def temp_grader_engine(test_db, grader_temp_creds):
    """Create SQLAlchemy engine with temp grader credentials."""
    user_config = test_db.admin.with_user(grader_temp_creds.username, grader_temp_creds.password)
    engine = create_engine(user_config.url())
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def grader_submit_server(test_grader_run, test_grader_critic_run):
    """Create grader submit server."""
    return GraderSubmitServer(grader_run_id=test_grader_run, critic_run_id=test_grader_critic_run)


async def test_grader_sql_basic_workflow(
    grader_submit_server,
    test_grader_run,
    test_grader_critic_run,
    test_snapshot,
    test_db,
    temp_grader_engine,
    tp_single_id,
    tp_single_occurrence_id,
    fp_id,
    fp_occurrence_id,
):
    """Test basic grader SQL workflow with TP, FP, and no-match edges."""
    # Simulate agent actions using temp user credentials

    with temp_grader_engine.connect() as conn:
        # Edge 1: TP match
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug,
                   tp_id, tp_occurrence_id, credit, rationale)
                VALUES (current_agent_run_id(), :critic_run, :issue_id, :snapshot,
                        :tp_id, :occ_id, :credit, :rationale)
            """),
            {
                "critic_run": str(test_grader_critic_run),
                "issue_id": "input-001",
                "snapshot": str(test_snapshot),
                "tp_id": tp_single_id,
                "occ_id": tp_single_occurrence_id,
                "credit": 0.8,
                "rationale": "Matches TP occurrence partially",
            },
        )

        # Edge 2: FP match
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug,
                   fp_id, fp_occurrence_id, credit, rationale)
                VALUES (current_agent_run_id(), :critic_run, :issue_id, :snapshot,
                        :fp_id, :occ_id, :credit, :rationale)
            """),
            {
                "critic_run": str(test_grader_critic_run),
                "issue_id": "input-002",
                "snapshot": str(test_snapshot),
                "fp_id": fp_id,
                "occ_id": fp_occurrence_id,
                "credit": 1.0,
                "rationale": "Matches known FP pattern",
            },
        )

        # Edge 3: No-match (still needs tp/fp columns, just NULL)
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug,
                   tp_id, tp_occurrence_id, credit, rationale)
                VALUES (current_agent_run_id(), :critic_run, :issue_id, :snapshot,
                        NULL, NULL, :credit, :rationale)
            """),
            {
                "critic_run": str(test_grader_critic_run),
                "issue_id": "input-003",
                "snapshot": str(test_snapshot),
                "credit": 0.0,
                "rationale": "No matching ground truth",
            },
        )

        conn.commit()

    # Agent calls submit tool
    tool_result = await grader_submit_server.submit_tool.run(
        {"summary": "Graded 3 input issues: 1 TP, 1 FP, 1 no-match"}
    )

    # Verify result
    result = tool_result.structured_content
    assert result["message"] == "Grading completed successfully with 3 edges"
    assert result["edges_count"] == 3
    assert result["input_issues_count"] == 3

    # Verify database state
    with get_session() as session:
        # Check edges exist
        edges = session.query(GradingEdge).filter_by(grader_run_id=test_grader_run).all()
        assert len(edges) == 3

        tp_edge = next(e for e in edges if e.critique_issue_id == "input-001")
        assert tp_edge.tp_id == tp_single_id
        assert tp_edge.tp_occurrence_id == tp_single_occurrence_id
        assert tp_edge.credit == 0.8

        fp_edge = next(e for e in edges if e.critique_issue_id == "input-002")
        assert fp_edge.fp_id == fp_id
        assert fp_edge.fp_occurrence_id == fp_occurrence_id
        assert fp_edge.credit == 1.0

        no_match = next(e for e in edges if e.critique_issue_id == "input-003")
        assert no_match.tp_id is None
        assert no_match.fp_id is None
        assert no_match.credit == 0.0


async def test_grader_sql_missing_edge_fails(
    grader_submit_server, test_grader_run, test_grader_critic_run, test_snapshot, test_db, temp_grader_engine
):
    """Test submit fails if any required edge is missing."""
    with temp_grader_engine.connect() as conn:
        # Only create edges for 2 out of 3 input issues
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :critic_run, :issue_id, :snapshot, :credit, :rationale)
            """),
            {
                "critic_run": str(test_grader_critic_run),
                "issue_id": "input-001",
                "snapshot": str(test_snapshot),
                "credit": 0.0,
                "rationale": "No match",
            },
        )

        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :critic_run, :issue_id, :snapshot, :credit, :rationale)
            """),
            {
                "critic_run": str(test_grader_critic_run),
                "issue_id": "input-002",
                "snapshot": str(test_snapshot),
                "credit": 0.0,
                "rationale": "No match",
            },
        )

        # Missing edge for input-003!
        conn.commit()

    # Submit should FAIL
    with pytest.raises(ToolError, match="Missing grading edges for input issues: input-003"):
        await grader_submit_server.submit_tool.run({"summary": "Incomplete grading"})


async def test_grader_sql_multiple_decisions_allowed(
    grader_submit_server,
    test_grader_run,
    test_db,
    temp_grader_engine,
    tp_occurrences_multi,
    tp_single_id,
    tp_single_occurrence_id,
):
    """Test that multiple decisions per input issue are allowed (partial credit to multiple TPs)."""

    with temp_grader_engine.connect() as conn:
        # Create valid decisions for input-001 and input-002
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
            """),
            {"input_id": "input-001", "credit": 0.0, "rationale": "No match"},
        )

        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
            """),
            {"input_id": "input-002", "credit": 0.0, "rationale": "No match"},
        )

        # Create TWO decisions for input-003 (partial matches to different TPs)
        # This is allowed: one input can match multiple ground truth issues with partial credit
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, tp_id, tp_occurrence_id,
                   credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :tp_id, :occ_id, :credit, :rationale)
            """),
            {
                "input_id": "input-003",
                "tp_id": tp_single_id,
                "occ_id": tp_single_occurrence_id,
                "credit": 0.3,
                "rationale": "Partially matches tp-001",
            },
        )

        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, tp_id, tp_occurrence_id,
                   credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :tp_id, :occ_id, :credit, :rationale)
            """),
            {
                "input_id": "input-003",
                "tp_id": tp_occurrences_multi[0][0],
                "occ_id": tp_occurrences_multi[0][1],
                "credit": 0.5,
                "rationale": "Also partially matches tp-002",
            },
        )

        conn.commit()

    # Submit should SUCCEED - multiple decisions per input are allowed
    tool_result = await grader_submit_server.submit_tool.run({"summary": "Multiple decisions allowed"})

    # Verify result
    result = tool_result.structured_content
    assert result["message"] == "Grading completed successfully with 4 decisions"
    assert result["decisions_count"] == 4  # 4 total decisions (1+1+2)
    assert result["input_issues_count"] == 3


async def test_grader_sql_rls_isolation(
    test_grader_run, test_grader_critic_run, test_snapshot, test_db, temp_grader_engine
):
    """Test RLS isolation - agents can only see their own run's decisions."""
    # Create another grader run (different agent)
    other_run_id = make_test_grader_run(test_grader_critic_run)

    # Insert decision from other run (using admin credentials)
    # First, add the input issues to reported_issues (required by check constraint)
    with get_session() as session:
        # Add "other-input" to the critic run's reported issues (for other grader run)
        other_issue = ReportedIssue(
            agent_run_id=test_grader_critic_run, issue_id="other-input", rationale="Other issue"
        )
        session.add(other_issue)

        # Add "my-input" to the critic run's reported issues (for test_grader_run via temp creds)
        my_issue = ReportedIssue(agent_run_id=test_grader_critic_run, issue_id="my-input", rationale="My issue")
        session.add(my_issue)

        session.flush()

        # Now add the edge for other run
        edge = GradingEdge(
            grader_run_id=other_run_id,
            critique_run_id=test_grader_critic_run,
            critique_issue_id="other-input",
            snapshot_slug=test_snapshot,
            credit=0.0,
            rationale="Other agent's edge",
        )
        session.add(edge)
        session.commit()

    # Now agent with temp_creds inserts their own decision

    with temp_grader_engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
            """),
            {"input_id": "my-input", "credit": 0.0, "rationale": "My decision"},
        )
        conn.commit()

        # Query should only see own run's data
        result = conn.execute(text("SELECT critique_issue_id FROM grading_edges ORDER BY critique_issue_id"))
        input_ids = [row[0] for row in result]

        # Should ONLY see "my-input", not "other-input"
        assert input_ids == ["my-input"]


async def test_grader_sql_credit_sum_trigger_enforcement(
    test_grader_run, test_grader_critic_run, test_db, temp_grader_engine, tp_single_id, tp_single_occurrence_id
):
    """Test SQL trigger prevents credit sum > 1.0 for same TP occurrence."""
    with temp_grader_engine.connect() as conn:
        # Decision for input-001: 0.7 credit to tp-shared/occ-shared
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, tp_id, tp_occurrence_id,
                   credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :tp_id, :occ_id, :credit, :rationale)
            """),
            {
                "input_id": "input-001",
                "tp_id": tp_single_id,
                "occ_id": tp_single_occurrence_id,
                "credit": 0.7,
                "rationale": "First match",
            },
        )
        conn.commit()

        # Decision for input-002: 0.5 credit to SAME tp-shared/occ-shared (total 1.2 > 1.0)
        # SQL trigger should REJECT this immediately on execute
        with pytest.raises(Exception, match=r"Credit sum would exceed 1\.0"):
            conn.execute(
                text("""
                    INSERT INTO grading_edges
                      (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, tp_id, tp_occurrence_id,
                       credit, rationale)
                    VALUES (current_agent_run_id(), :input_id, :tp_id, :occ_id, :credit, :rationale)
                """),
                {
                    "input_id": "input-002",
                    "tp_id": tp_single_id,
                    "occ_id": tp_single_occurrence_id,
                    "credit": 0.5,
                    "rationale": "Second match (would exceed 1.0)",
                },
            )


async def test_grader_sql_hard_delete_revision_workflow(
    grader_submit_server, test_grader_run, test_db, temp_grader_engine
):
    """Test hard delete workflow - delete incorrect decision and replace with new one."""
    with temp_grader_engine.connect() as conn:
        # Create active decisions for all 3 input issues
        for i in range(1, 4):
            conn.execute(
                text("""
                    INSERT INTO grading_edges
                      (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                    VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
                """),
                {"input_id": f"input-00{i}", "credit": 0.0, "rationale": f"Decision {i}"},
            )

        # Hard delete decision for input-002 (agent reconsidered)
        conn.execute(
            text("""
                DELETE FROM grading_edges
                WHERE agent_run_id = current_agent_run_id()
                  AND critique_issue_id = :input_id
            """),
            {"input_id": "input-002"},
        )

        conn.commit()

    # Submit should require a NEW decision for input-002 (deleted one doesn't count)
    with pytest.raises(ToolError, match="Missing grading decisions for input issues: input-002"):
        await grader_submit_server.submit_tool.run({"summary": "Missing decision due to deletion"})

    # Now create new active decision for input-002

    with temp_grader_engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO grading_edges
                  (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
            """),
            {"input_id": "input-002", "credit": 0.0, "rationale": "Revised decision"},
        )
        conn.commit()

    # Now submit should succeed
    tool_result = await grader_submit_server.submit_tool.run({"summary": "All decisions finalized (1 revised)"})

    result = tool_result.structured_content
    assert result["decisions_count"] == 3  # 3 active decisions
    assert result["input_issues_count"] == 3


# =============================================================================
# Report Failure Tool Tests
# =============================================================================


async def test_grader_report_failure_basic(grader_submit_server, test_grader_run, test_db):
    """Test report_failure tool marks run as failed with reason."""
    # Call report_failure tool
    await grader_submit_server.report_failure_tool.run(
        {"message": "Cannot grade: critic output is malformed and contains no parseable issues"}
    )

    # Verify database state
    with get_session() as session:
        grader_run = session.get(AgentRun, test_grader_run)
        assert grader_run is not None
        assert grader_run.status == AgentRunStatus.REPORTED_FAILURE
        assert (
            grader_run.completion_summary == "Cannot grade: critic output is malformed and contains no parseable issues"
        )


async def test_grader_report_failure_prevents_subsequent_submit(
    grader_submit_server, test_grader_run, test_db, temp_grader_engine
):
    """Test that submit fails after report_failure has been called."""
    # First report failure
    await grader_submit_server.report_failure_tool.run({"message": "Grading not possible"})

    # Then try to submit - should fail because run already reported failure
    with pytest.raises(ToolError, match="already reported failure"):
        await grader_submit_server.submit_tool.run({"summary": "Attempting late submit"})


async def test_grader_report_failure_after_complete_fails(
    grader_submit_server, test_grader_run, test_db, temp_grader_engine
):
    """Test that report_failure fails if run is already completed."""
    # First complete the run by adding decisions and submitting
    with temp_grader_engine.connect() as conn:
        for i in range(1, 4):
            conn.execute(
                text("""
                    INSERT INTO grading_edges
                      (grader_run_id, critique_run_id, critique_issue_id, snapshot_slug, credit, rationale)
                    VALUES (current_agent_run_id(), :input_id, :credit, :rationale)
                """),
                {"input_id": f"input-00{i}", "credit": 0.0, "rationale": f"Decision {i}"},
            )
        conn.commit()

    # Submit successfully
    await grader_submit_server.submit_tool.run({"summary": "Completed grading"})

    # Then try to report failure - should fail because already completed
    with pytest.raises(ToolError, match="already completed"):
        await grader_submit_server.report_failure_tool.run({"message": "Trying to fail after completion"})


async def test_grader_report_failure_idempotency_fails(grader_submit_server, test_grader_run, test_db):
    """Test that calling report_failure twice fails (not idempotent)."""
    # First call succeeds
    await grader_submit_server.report_failure_tool.run({"message": "First failure report"})

    # Second call should fail
    with pytest.raises(ToolError, match="already reported failure"):
        await grader_submit_server.report_failure_tool.run({"message": "Second failure report"})
