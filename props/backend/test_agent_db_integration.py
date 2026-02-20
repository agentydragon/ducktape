"""Integration tests for get_agent_db with real Postgres and RLS.

Verifies that get_agent_db returns a Database instance whose sessions
are subject to RLS policies. Uses the same two-user pattern as
test_split_based_rls: admin writes test data, agent queries with RLS.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import pytest_bazel

from props.backend.auth import AdminIdentity, AgentIdentity, get_agent_db
from props.core.agent_types import CriticTypeConfig
from props.db.database import Database
from props.db.examples import Example
from props.db.models import AgentRun, AgentRunStatus
from props.orchestration.agent_credentials import AgentCredentials
from props.testing.constants import DEFAULT_TEST_MODEL
from props.testing.fixtures.credentials import make_agent_credentials
from props.testing.fixtures.runs import FAKE_CRITIC_DIGEST, ensure_fake_agent_definitions

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def critic_agent_creds(synced_db: Database) -> AsyncGenerator[tuple[AgentCredentials, CriticTypeConfig]]:
    """Create critic agent credentials with a real Postgres role."""
    type_config = CriticTypeConfig(example={"snapshot_slug": "test-fixtures/train1", "kind": "whole_snapshot"})
    creds = await make_agent_credentials(synced_db, type_config, FAKE_CRITIC_DIGEST)
    yield creds, type_config


async def test_agent_db_returns_rls_scoped_database(
    synced_db: Database, critic_agent_creds: tuple[AgentCredentials, CriticTypeConfig]
) -> None:
    """get_agent_db with agent auth returns a Database that enforces RLS.

    Writes a critic run for a VALID split snapshot as admin, then verifies
    the agent Database cannot see it (critics can only see their own runs).
    """
    creds, type_config = critic_agent_creds
    # Setup: create a critic run for VALID split as admin
    valid_run_id = uuid4()
    with synced_db.session() as session:
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/valid1").first()
        assert example, "test-fixtures/valid1 fixture not found"

        ensure_fake_agent_definitions(session)
        type_config = CriticTypeConfig(example=example.to_example_spec())
        admin_run = AgentRun(
            agent_run_id=valid_run_id,
            image_digest=FAKE_CRITIC_DIGEST,
            model=DEFAULT_TEST_MODEL,
            status=AgentRunStatus.EXITED,
            type_config=type_config.model_dump(),
            budget_usd=5.0,
        )
        session.add(admin_run)
        session.commit()

    # Exercise: get_agent_db with agent auth
    auth = AgentIdentity(
        agent_type=type_config.agent_type,
        agent_run_id=uuid4(),  # The auth context run_id (doesn't need to match creds run)
        username=creds.username,
        password=creds.password,
    )
    gen = get_agent_db(admin_db=synced_db, auth=auth)
    agent_db = next(gen)

    try:
        assert agent_db is not synced_db, "Agent should get a separate Database instance"

        # Verify RLS: agent cannot see the VALID split run (not their own run)
        with agent_db.session() as session:
            visible_run = session.get(AgentRun, valid_run_id)
            assert visible_run is None, "Agent should NOT see another agent's run via RLS"
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


async def test_agent_db_can_see_own_run(
    synced_db: Database, critic_agent_creds: tuple[AgentCredentials, CriticTypeConfig]
) -> None:
    """Agent Database can see the agent's own run record."""
    creds, type_config = critic_agent_creds
    # The fixture already created a run for this agent's role.
    # Find the run_id from the username (agent_{uuid}).
    agent_run_id_str = creds.username.removeprefix("agent_")
    agent_run_id = UUID(agent_run_id_str)

    auth = AgentIdentity(
        agent_type=type_config.agent_type, agent_run_id=agent_run_id, username=creds.username, password=creds.password
    )
    gen = get_agent_db(admin_db=synced_db, auth=auth)
    agent_db = next(gen)

    try:
        with agent_db.session() as session:
            own_run = session.get(AgentRun, agent_run_id)
            assert own_run is not None, "Agent should see their own run via RLS"
            assert own_run.agent_run_id == agent_run_id
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


async def test_admin_auth_returns_admin_db(synced_db: Database, exhaust_generator) -> None:
    """get_agent_db with admin auth returns the admin Database directly."""
    auth = AdminIdentity(username=synced_db.config.user, password=synced_db.config.password)

    gen = get_agent_db(admin_db=synced_db, auth=auth)
    db = exhaust_generator(gen)

    assert db is synced_db, "Admin should get the same admin Database instance"


if __name__ == "__main__":
    pytest_bazel.main()
