"""Shared fixtures and helpers for critic integration tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRunStatus
from props.db.snapshots import LocationAnchor
from props.orchestration.agent_credentials import AgentCredentials, ensure_agent_role
from props.testing.fixtures.runs import make_fake_critic_run

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

pytestmark = [pytest.mark.integration]


@pytest.fixture
def test_critic_run(synced_db: Database, test_snapshot):
    """Create test critic run.

    Returns:
        UUID of the created critic run
    """
    with synced_db.session() as session:
        critic_run = make_fake_critic_run(
            session=session,
            example=WholeSnapshotExample(snapshot_slug=test_snapshot),
            status=AgentRunStatus.IN_PROGRESS,
        )
        session.add(critic_run)
        session.commit()

        return critic_run.agent_run_id


@pytest.fixture
async def temp_creds(synced_db: Database, test_critic_run) -> AgentCredentials:
    """Ensure agent role exists with RLS scoping.

    Returns:
        AgentCredentials for the critic agent
    """
    return await ensure_agent_role(synced_db.config, test_critic_run)


@pytest.fixture
def temp_engine(synced_db: Database, temp_creds) -> Engine:
    """Create SQLAlchemy engine using temporary user credentials.

    Returns:
        SQLAlchemy Engine connected as the temporary user
    """
    user_config = synced_db.config.with_user(temp_creds.username, temp_creds.password)
    return create_engine(user_config.url)


def insert_issue(conn: Connection, issue_id: str, rationale: str) -> None:
    """Insert a reported issue using temp user credentials.

    Args:
        conn: Database connection (must be from temp user engine)
        issue_id: Issue identifier
        rationale: Issue rationale
    """
    conn.execute(
        text("""
            INSERT INTO reported_issues (agent_run_id, issue_id, rationale)
            VALUES (current_agent_run_id(), :issue_id, :rationale)
        """),
        {"issue_id": issue_id, "rationale": rationale},
    )


def insert_occurrence(conn: Connection, issue_id: str, locations: list[LocationAnchor]) -> None:
    """Insert a reported issue occurrence using temp user credentials.

    Args:
        conn: Database connection (must be from temp user engine)
        issue_id: Issue ID this occurrence belongs to
        locations: List of LocationAnchor objects specifying where the occurrence is
    """
    locations_json = json.dumps([loc.model_dump(exclude_none=True) for loc in locations])
    conn.execute(
        text("""
            INSERT INTO reported_issue_occurrences
              (agent_run_id, reported_issue_id, locations)
            VALUES (current_agent_run_id(), :issue_id, CAST(:locations AS jsonb))
        """),
        {"issue_id": issue_id, "locations": locations_json},
    )
