"""Which replica projects a sandbox: a lease, and the fence every projection write takes.

These run in the caller's session; the caller owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.threads.models import EventLog, SandboxProjectionLease


@dataclass(frozen=True)
class ProjectionLease:
    sandbox: str
    token: UUID


class ProjectionLeaseLostError(Exception):
    """The app replica no longer owns authority to commit projections for this sandbox."""


async def acquire(session: AsyncSession, sandbox: str, duration: timedelta) -> ProjectionLease | None:
    _positive_duration(duration)
    token = uuid4()
    acquired = await session.scalar(
        insert(SandboxProjectionLease)
        .values(sandbox=sandbox, token=token, expires_at=func.clock_timestamp() + duration)
        .on_conflict_do_update(
            index_elements=[SandboxProjectionLease.sandbox],
            set_={"token": token, "expires_at": func.clock_timestamp() + duration},
            where=SandboxProjectionLease.expires_at <= func.clock_timestamp(),
        )
        .returning(SandboxProjectionLease.token)
    )
    return ProjectionLease(sandbox, token) if acquired is not None else None


async def renew(session: AsyncSession, lease: ProjectionLease, duration: timedelta) -> bool:
    _positive_duration(duration)
    renewed = await session.scalar(
        update(SandboxProjectionLease)
        .where(
            SandboxProjectionLease.sandbox == lease.sandbox,
            SandboxProjectionLease.token == lease.token,
            SandboxProjectionLease.expires_at > func.clock_timestamp(),
        )
        .values(expires_at=func.clock_timestamp() + duration)
        .returning(SandboxProjectionLease.token)
    )
    return renewed is not None


async def release(session: AsyncSession, lease: ProjectionLease) -> None:
    await session.execute(
        delete(SandboxProjectionLease).where(
            SandboxProjectionLease.sandbox == lease.sandbox, SandboxProjectionLease.token == lease.token
        )
    )


def _positive_duration(duration: timedelta) -> None:
    if duration <= timedelta(0):
        raise ValueError("projection lease duration must be positive")


async def fence(session: AsyncSession, lease: ProjectionLease, thread_id: UUID) -> None:
    # Lock before reading database time: a transaction that waited on an owner must not rely on
    # its transaction-start timestamp. Takeover/renewal waits until this write commits or rolls back.
    owned = await session.scalar(
        select(SandboxProjectionLease).where(SandboxProjectionLease.sandbox == lease.sandbox).with_for_update()
    )
    now = (await session.scalars(select(func.clock_timestamp()))).one()
    if owned is None or owned.token != lease.token or owned.expires_at <= now:
        raise ProjectionLeaseLostError(lease.sandbox)
    sandbox = await session.scalar(select(EventLog.sandbox).where(EventLog.id == thread_id))
    if sandbox != lease.sandbox:
        raise ProjectionLeaseLostError("lease does not own this thread's sandbox")
