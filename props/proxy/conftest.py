"""Shared test fixtures and utilities for proxy tests.

Provides common fixtures for:
- Test client creation
- Database fixtures (examples, agent runs)
- Temp user credentials for agent auth
- Basic auth header generation
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Generator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from starlette.testclient import TestClient

from props.db.config import DatabaseConfig
from props.db.examples import Example
from props.db.models import AgentRunStatus
from props.db.session import get_session
from props.db.temp_user_manager import TempUserCredentials, TempUserManager
from props.proxy.app import app
from props.testing.fixtures import make_critic_run


def basic_auth_header(username: str, password: str) -> str:
    """Create Basic auth header value."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {credentials}"


@pytest.fixture
def client() -> Generator[TestClient]:
    """Create test client for the combined proxy."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def train1_example(synced_test_db: DatabaseConfig) -> Example:
    """Provide an Example from the train1 fixture."""
    with get_session() as session:
        example = session.query(Example).filter_by(snapshot_slug="test-fixtures/train1").first()
        assert example, "test-fixtures/train1 fixture not found"
        # Detach from session for use in other transactions
        session.expunge(example)
        return example


@pytest.fixture
def agent_run_id(synced_test_db: DatabaseConfig, train1_example: Example) -> UUID:
    """Create an in-progress agent run and return its ID."""
    run_id = uuid4()
    with get_session() as session:
        agent_run = make_critic_run(
            example=train1_example, model="gpt-4o", status=AgentRunStatus.IN_PROGRESS, agent_run_id=run_id
        )
        session.add(agent_run)
        session.commit()
    return run_id


@pytest_asyncio.fixture
async def agent_creds(synced_test_db: DatabaseConfig, agent_run_id: UUID) -> AsyncGenerator[TempUserCredentials]:
    """Provide temp user credentials for the agent run."""
    async with TempUserManager(synced_test_db.admin, agent_run_id) as creds:
        yield creds
