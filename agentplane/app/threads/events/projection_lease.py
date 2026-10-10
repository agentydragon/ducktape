"""Which replica projects a Session: a lease, and the fence every projection write takes.

These run in the caller's session; the caller owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.threads.models import SessionProjectionLease


@dataclass(frozen=True)
class ProjectionLease:
    session_id: UUID
    token: UUID


class ProjectionLeaseLostError(Exception):
    """The app replica no longer owns authority to commit projections for this Session."""


async def acquire(session: AsyncSession, session_id: UUID, duration: timedelta) -> ProjectionLease | None:
    _positive_duration(duration)
    token = uuid4()
    acquired = await session.scalar(
        insert(SessionProjectionLease)
        .values(session_id=session_id, token=token, expires_at=func.clock_timestamp() + duration)
        .on_conflict_do_update(
            index_elements=[SessionProjectionLease.session_id],
            set_={"token": token, "expires_at": func.clock_timestamp() + duration},
            where=SessionProjectionLease.expires_at <= func.clock_timestamp(),
        )
        .returning(SessionProjectionLease.token)
    )
    return ProjectionLease(session_id, token) if acquired is not None else None


async def renew(session: AsyncSession, lease: ProjectionLease, duration: timedelta) -> bool:
    _positive_duration(duration)
    renewed = await session.scalar(
        update(SessionProjectionLease)
        .where(
            SessionProjectionLease.session_id == lease.session_id,
            SessionProjectionLease.token == lease.token,
            SessionProjectionLease.expires_at > func.clock_timestamp(),
        )
        .values(expires_at=func.clock_timestamp() + duration)
        .returning(SessionProjectionLease.token)
    )
    return renewed is not None


async def release(session: AsyncSession, lease: ProjectionLease) -> None:
    await session.execute(
        delete(SessionProjectionLease).where(
            SessionProjectionLease.session_id == lease.session_id, SessionProjectionLease.token == lease.token
        )
    )


def _positive_duration(duration: timedelta) -> None:
    if duration <= timedelta(0):
        raise ValueError("projection lease duration must be positive")


async def fence(session: AsyncSession, lease: ProjectionLease, thread_id: UUID) -> None:
    # Lock before reading database time: a transaction that waited on an owner must not rely on
    # its transaction-start timestamp. Takeover/renewal waits until this write commits or rolls back.
    owned = await session.scalar(
        select(SessionProjectionLease).where(SessionProjectionLease.session_id == lease.session_id).with_for_update()
    )
    now = (await session.scalars(select(func.clock_timestamp()))).one()
    if owned is None or owned.token != lease.token or owned.expires_at <= now:
        raise ProjectionLeaseLostError(lease.session_id)
    if thread_id != lease.session_id:
        raise ProjectionLeaseLostError("lease does not own this Session")
