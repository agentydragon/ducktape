"""Runner channel connection ownership: the newest accepted connection for a Sandbox wins."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.sandbox_service.commands import db


class ConnectionSupersededError(Exception):
    """A newer connection for the same Sandbox was accepted."""


class ConnectionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def accept(self, *, sandbox_uid: UUID, pod_uid: str, owner_replica: str, runner_boot_id: str) -> int:
        """Take ownership for a new connection and return its epoch, committed before `Welcome`."""
        values = {
            "pod_uid": pod_uid,
            "owner_replica": owner_replica,
            "runner_boot_id": runner_boot_id,
            "last_seen_at": func.now(),
        }
        async with self._sessions.begin() as session:
            result = await session.execute(
                insert(db.RunnerConnection)
                .values(sandbox_uid=sandbox_uid, connection_epoch=1, **values)
                .on_conflict_do_update(
                    index_elements=[db.RunnerConnection.sandbox_uid],
                    set_={"connection_epoch": db.RunnerConnection.connection_epoch + 1, **values},
                )
                .returning(db.RunnerConnection.connection_epoch)
            )
            return result.scalar_one()

    async def touch(self, *, sandbox_uid: UUID, epoch: int, seen_at: datetime) -> None:
        """Record liveness for the connection holding `epoch`, or learn that it no longer does."""
        async with self._sessions.begin() as session:
            touched = await session.scalar(
                update(db.RunnerConnection)
                .where(db.RunnerConnection.sandbox_uid == sandbox_uid, db.RunnerConnection.connection_epoch == epoch)
                .values(last_seen_at=seen_at)
                .returning(db.RunnerConnection.sandbox_uid)
            )
        if touched is None:
            raise ConnectionSupersededError(sandbox_uid)
