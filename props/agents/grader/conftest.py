"""Fixtures and helpers for grader tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from props.db.database import Database


@pytest.fixture
def session(synced_db: Database) -> Generator[Session]:
    """Alias for synced_test_session - provides a database session over synced test DB."""
    with synced_db.session() as sess:
        yield sess
