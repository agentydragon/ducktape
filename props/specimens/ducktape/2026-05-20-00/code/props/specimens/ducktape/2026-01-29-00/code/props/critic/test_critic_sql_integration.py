"""Integration test for critic SQL workflow.

Tests the SQL workflow where critic agents:
1. Get temporary database credentials with RLS scoping
2. Write reported issues directly to PostgreSQL (using locations JSONB array)
"""

from __future__ import annotations

import pytest
import pytest_bazel
from sqlalchemy import text

from props.core.ids import SnapshotSlug
from props.critic.conftest import insert_issue, insert_occurrence
from props.db.examples import Example
from props.db.models import AgentRunStatus, ReportedIssue, ReportedIssueOccurrence
from props.db.session import get_session
from props.db.snapshots import DBLocationAnchor
from props.testing.fixtures.runs import make_critic_run

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


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


async def test_insert_issue(test_critic_run, temp_engine):
    """Test insert_issue helper."""

    with temp_engine.connect() as conn:
        insert_issue(conn, "test-issue", "Test rationale")
        conn.commit()

    with get_session() as session:
        issue = session.query(ReportedIssue).filter_by(agent_run_id=test_critic_run, issue_id="test-issue").one()
        assert issue.rationale == "Test rationale"


async def test_insert_occurrence(test_critic_run, temp_engine):
    """Test insert_occurrence helper with locations."""

    with temp_engine.connect() as conn:
        insert_issue(conn, "test-issue", "Test rationale")
        insert_occurrence(conn, "test-issue", [DBLocationAnchor(file="add.py", start_line=1, end_line=3)])
        conn.commit()

    with get_session() as session:
        occ = (
            session.query(ReportedIssueOccurrence)
            .filter_by(agent_run_id=test_critic_run, reported_issue_id="test-issue")
            .one()
        )
        assert occ.locations == [DBLocationAnchor(file="add.py", start_line=1, end_line=3)]


async def test_insert_multi_location_occurrence(test_critic_run, temp_engine):
    """Test multi-location occurrence (e.g., duplicated code)."""

    with temp_engine.connect() as conn:
        insert_issue(conn, "duplicated-enum", "Status enum duplicated across files")
        insert_occurrence(
            conn,
            "duplicated-enum",
            [
                DBLocationAnchor(file="add.py", start_line=1, end_line=3),
                DBLocationAnchor(file="subtract.py", start_line=1, end_line=3),
            ],
        )
        conn.commit()

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


if __name__ == "__main__":
    pytest_bazel.main()
