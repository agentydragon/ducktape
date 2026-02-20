"""Unit tests for auth module - get_agent_db credential passthrough."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_bazel
from fastapi import HTTPException

from props.backend.auth import AdminIdentity, AgentIdentity, AnonymousIdentity, get_agent_db
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentType


def test_get_agent_db_admin_returns_admin_db(exhaust_generator):
    """Admin users get the shared admin database connection."""
    admin_db = MagicMock(spec=Database)
    auth = AdminIdentity(username="postgres", password="secret")

    gen = get_agent_db(admin_db=admin_db, auth=auth)
    db = exhaust_generator(gen)
    assert db is admin_db


def test_get_agent_db_anonymous_raises_401():
    """Anonymous (unauthenticated) callers get 401."""
    admin_db = MagicMock(spec=Database)
    auth = AnonymousIdentity()

    gen = get_agent_db(admin_db=admin_db, auth=auth)
    with pytest.raises(HTTPException) as exc_info:
        next(gen)
    assert exc_info.value.status_code == 401


def test_get_agent_db_agent_calls_per_request():
    """Agent callers get a Database.per_request() instance with their credentials."""
    run_id = uuid4()
    admin_config = DatabaseConfig(host="localhost", port=5432, database="testdb", user="admin", password="admin_pass")
    admin_db = MagicMock(spec=Database)
    admin_db.config = admin_config

    auth = AgentIdentity(
        agent_type=AgentType.CRITIC_DEV_OPTIMIZE, agent_run_id=run_id, username=f"agent_{run_id}", password="agent_pass"
    )

    mock_agent_db = MagicMock(spec=Database)
    with patch.object(Database, "per_request", return_value=mock_agent_db) as mock_pr:
        gen = get_agent_db(admin_db=admin_db, auth=auth)
        db = next(gen)

        # Verify per_request was called with agent credentials
        mock_pr.assert_called_once()
        config = mock_pr.call_args[0][0]
        assert config.user == f"agent_{run_id}"
        assert config.password == "agent_pass"
        assert config.host == "localhost"
        assert config.database == "testdb"

        # Verify it's the per_request instance, not admin
        assert db is mock_agent_db
        assert db is not admin_db

        # Cleanup
        with contextlib.suppress(StopIteration):
            next(gen)


def test_get_agent_db_agent_disposes_on_cleanup():
    """Agent per-request database is disposed after the request."""
    run_id = uuid4()
    admin_config = DatabaseConfig(host="localhost", port=5432, database="testdb", user="admin", password="admin_pass")
    admin_db = MagicMock(spec=Database)
    admin_db.config = admin_config

    auth = AgentIdentity(
        agent_type=AgentType.CRITIC_DEV_OPTIMIZE, agent_run_id=run_id, username=f"agent_{run_id}", password="agent_pass"
    )

    mock_agent_db = MagicMock(spec=Database)
    with patch.object(Database, "per_request", return_value=mock_agent_db):
        gen = get_agent_db(admin_db=admin_db, auth=auth)
        next(gen)  # Get the yielded db

        mock_agent_db.dispose.assert_not_called()

        # Exhaust the generator (triggers finally block)
        with contextlib.suppress(StopIteration):
            next(gen)

        mock_agent_db.dispose.assert_called_once()


if __name__ == "__main__":
    pytest_bazel.main()
