"""What counts as a re-runnable database abort, and that the retry re-runs only those.

The transient classifier is checked against errors a real Postgres raises — a genuine deadlock and a
genuine constraint violation — not hand-built stand-ins, so it cannot drift from what SQLAlchemy's
asyncpg dialect actually surfaces.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_retry import retry_transient_db, transient_database_error
from haku.console.database_schema import Conversation
from haku.console.harnesses.kind import HarnessKind


async def test_a_real_postgres_deadlock_is_transient(migrated_sessions: async_sessionmaker[AsyncSession]) -> None:
    """Two transactions take two advisory xact locks in opposite orders, a barrier holding both first
    locks until both are held, so Postgres aborts one with a genuine SQLSTATE 40P01."""
    barrier = asyncio.Barrier(2)

    async def cross_lock(first: int, second: int) -> None:
        async with migrated_sessions.begin() as db:
            await db.execute(select(func.pg_advisory_xact_lock(first)))
            await barrier.wait()
            await db.execute(select(func.pg_advisory_xact_lock(second)))

    outcomes = await asyncio.gather(cross_lock(1, 2), cross_lock(2, 1), return_exceptions=True)
    error = one(outcome for outcome in outcomes if isinstance(outcome, BaseException))
    assert transient_database_error(error)


async def test_an_integrity_violation_is_not_transient(migrated_sessions: async_sessionmaker[AsyncSession]) -> None:
    """A constraint violation fails identically on retry, so it is the work's own failure — never retried."""
    with pytest.raises(IntegrityError) as excinfo:
        async with migrated_sessions.begin() as db:
            db.add(
                Conversation(
                    conversation_id=uuid4(),
                    operator_id=uuid4(),  # references no operator row, so the INSERT is a foreign-key violation
                    harness_kind=HarnessKind.CLAUDE_CODE,
                    created_at=datetime.now(UTC),
                )
            )
    assert not transient_database_error(excinfo.value)


def test_a_non_database_error_is_not_transient() -> None:
    assert not transient_database_error(ValueError("not a database error at all"))


async def test_retry_re_runs_a_transient_abort_until_it_succeeds() -> None:
    attempts = 0

    @retry_transient_db
    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError("UPDATE conversation SET next_event_seq=%s", {}, Exception("deadlock detected"))
        return "committed"

    assert await op() == "committed"
    assert attempts == 3


async def test_retry_does_not_swallow_a_real_fault() -> None:
    attempts = 0

    @retry_transient_db
    async def op() -> None:
        nonlocal attempts
        attempts += 1
        raise IntegrityError("INSERT", {}, Exception("unique violation"))

    with pytest.raises(IntegrityError):
        await op()
    assert attempts == 1  # a non-transient fault propagates on the first raise, never retried


if __name__ == "__main__":
    pytest_bazel.main()
