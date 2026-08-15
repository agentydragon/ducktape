"""PostgreSQL test database utilities: create and force-drop per-test databases."""

from __future__ import annotations

import re
from collections.abc import Sequence

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


def create_database_sync(admin_url: str, db_name: str, *, extensions: Sequence[str] = ()) -> str:
    """Create a per-test database and its extensions, returning its URL.

    Extensions are installed here rather than by whatever runs migrations because the interesting
    ones are untrusted — pgvector among them — so only a superuser can install them, which in a
    deployment means the operator that owns the cluster rather than the application's own role.
    """
    _require_safe_db_name(db_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()

    db_url = admin_url.rsplit("/", 1)[0] + f"/{db_name}"
    if extensions:
        engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                for extension in extensions:
                    conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
        finally:
            engine.dispose()
    return db_url


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
