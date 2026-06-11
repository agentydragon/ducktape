"""PostgreSQL test database utilities: create and force-drop per-test databases."""

from __future__ import annotations

import re

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

_TERMINATE_SQL = text(
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :db_name AND pid <> pg_backend_pid()"
)

_SAFE_DB_NAME = re.compile(r"^[a-zA-Z0-9_]+$")


def _require_safe_db_name(db_name: str) -> None:
    """Raise if db_name contains characters outside [a-zA-Z0-9_]."""
    if not _SAFE_DB_NAME.match(db_name):
        raise ValueError(f"Unsafe database name for test teardown: {db_name!r} (must match [a-zA-Z0-9_]+)")


async def force_drop_database(admin_url: str, db_name: str) -> None:
    """Terminate all connections to db_name and drop it (async).

    Creates a temporary AUTOCOMMIT engine from admin_url. Intended for test teardown.
    """
    _require_safe_db_name(db_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(_TERMINATE_SQL, {"db_name": db_name})
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        await engine.dispose()


def force_drop_database_sync(admin_url: str, db_name: str) -> None:
    """Terminate all connections to db_name and drop it (sync).

    Creates a temporary AUTOCOMMIT engine from admin_url. Intended for test teardown.
    """
    _require_safe_db_name(db_name)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(_TERMINATE_SQL, {"db_name": db_name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        engine.dispose()
