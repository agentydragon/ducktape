"""FastAPI dependencies for database access."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from props.db.database import Database


def get_admin_db(request: Request) -> Database:
    """Get admin Database instance from app state.

    Uses the server's admin connection pool. Use for operations that
    genuinely require admin privileges (INSERT into llm_requests,
    launching agent runs, etc).
    """
    return request.app.state.admin_db  # type: ignore[no-any-return]


# Type alias for admin database dependency (use in FastAPI route signatures)
AdminDb = Annotated[Database, Depends(get_admin_db)]
