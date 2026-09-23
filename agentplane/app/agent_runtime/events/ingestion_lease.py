"""Which replica ingests a sandbox: a lease, and the fence every write under it takes.

These run in the caller's session; `ThreadStore` owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.thread.models import EventLog, SandboxIngestion


@dataclass(frozen=True)
class IngestionLease:
    sandbox: str
    token: UUID


class IngestionLeaseLostError(Exception):
    """The sandbox ingester no longer owns authority to commit observations."""


async def acquire(session: AsyncSession, sandbox: str, duration: timedelta) -> IngestionLease | None:
    _positive_duration(duration)
    token = uuid4()
    acquired = await session.scalar(
        insert(SandboxIngestion)
        .values(sandbox=sandbox, token=token, expires_at=func.clock_timestamp() + duration)
        .on_conflict_do_update(
            index_elements=[SandboxIngestion.sandbox],
            set_={"token": token, "expires_at": func.clock_timestamp() + duration},
            where=SandboxIngestion.expires_at <= func.clock_timestamp(),
        )
        .returning(SandboxIngestion.token)
    )
    return IngestionLease(sandbox, token) if acquired is not None else None


async def renew(session: AsyncSession, lease: IngestionLease, duration: timedelta) -> bool:
    _positive_duration(duration)
    renewed = await session.scalar(
        update(SandboxIngestion)
        .where(
            SandboxIngestion.sandbox == lease.sandbox,
            SandboxIngestion.token == lease.token,
            SandboxIngestion.expires_at > func.clock_timestamp(),
        )
        .values(expires_at=func.clock_timestamp() + duration)
        .returning(SandboxIngestion.token)
    )
    return renewed is not None


async def release(session: AsyncSession, lease: IngestionLease) -> None:
    await session.execute(
        delete(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox, SandboxIngestion.token == lease.token)
    )


def _positive_duration(duration: timedelta) -> None:
    if duration <= timedelta(0):
        raise ValueError("ingestion lease duration must be positive")


async def fence(session: AsyncSession, lease: IngestionLease, thread_id: UUID) -> None:
    # Lock before reading database time: a transaction that waited on an owner must not rely on
    # its transaction-start timestamp. Takeover/renewal waits until this write commits or rolls back.
    owned = await session.scalar(
        select(SandboxIngestion).where(SandboxIngestion.sandbox == lease.sandbox).with_for_update()
    )
    now = (await session.scalars(select(func.clock_timestamp()))).one()
    if owned is None or owned.token != lease.token or owned.expires_at <= now:
        raise IngestionLeaseLostError(lease.sandbox)
    sandbox = await session.scalar(select(EventLog.sandbox).where(EventLog.id == thread_id))
    if sandbox != lease.sandbox:
        raise IngestionLeaseLostError("lease does not own this thread's sandbox")
