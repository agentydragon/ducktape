"""The channel operations each Sandbox's runner connections have agreed.

Who holds a connection, its epoch and its liveness live in the Sandbox's Lease
(`agentplane.sandbox_service.connection_records`).
"""

from collections.abc import Set
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.sandbox_service.commands import db


class ConnectionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def record_operations(self, sandbox_uid: UUID, operations: Set[str]) -> None:
        """Add a connection's agreed `operations` to the Sandbox's, committed before `Welcome`."""
        async with self._sessions.begin() as session:
            await session.execute(
                insert(db.RunnerConnection)
                .values(sandbox_uid=sandbox_uid, operations=sorted(operations))
                .on_conflict_do_update(
                    index_elements=[db.RunnerConnection.sandbox_uid],
                    set_={
                        "operations": text(
                            "ARRAY(SELECT DISTINCT unnest(runner_connection.operations || excluded.operations) "
                            "ORDER BY 1)"
                        )
                    },
                )
            )

    async def read_operations(self, sandbox_uid: UUID) -> frozenset[str]:
        """The operations any connection of this Sandbox has agreed, whether or not one is live now."""
        async with self._sessions() as session:
            operations = await session.scalar(
                select(db.RunnerConnection.operations).where(db.RunnerConnection.sandbox_uid == sandbox_uid)
            )
        return frozenset(operations or ())
