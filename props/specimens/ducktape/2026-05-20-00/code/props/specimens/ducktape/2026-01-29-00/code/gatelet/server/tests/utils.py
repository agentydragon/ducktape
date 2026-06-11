"""Test utilities for Gatelet tests."""

from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def persist[T](db_session: AsyncSession, obj: T) -> T:
    """Persist a model instance and refresh it.

    This function works both within an existing transaction
    and when no transaction is present.
    """
    db_session.add(obj)

    # Only commit if not in a transaction
    # When in a transaction, the session fixture will handle the commit/rollback
    if not db_session.in_transaction():
        await db_session.commit()
    else:
        await db_session.flush()

    await db_session.refresh(obj)
    return obj
