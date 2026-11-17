"""Transactional credentials and recovery fences for provider sessions."""

import threading
import time
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import Boolean, Column, Integer, LargeBinary, MetaData, String, Table, select, update
from sqlalchemy.dialects.sqlite import insert

from platform_sessions.storage import Storage

_metadata = MetaData()
_sessions = Table(
    "sessions",
    _metadata,
    Column("name", String, primary_key=True),
    Column("credentials", LargeBinary),
    Column("generation", Integer),
    Column("pending", Boolean, nullable=False),
)


class ProviderSessionStore[T]:
    def __init__(self, directory: Path, name: str, adapter: TypeAdapter[T]):
        self.directory = directory
        self.storage = Storage(directory / "imports")
        directory.chmod(0o700)
        self.name = name
        self.lock = threading.Lock()
        self.adapter = adapter
        with self.storage.transaction() as connection:
            _metadata.create_all(connection)
            connection.execute(insert(_sessions).values(name=name, pending=False).on_conflict_do_nothing())

    def load(self) -> T | None:
        return self.snapshot()[0]

    def snapshot(self) -> tuple[T | None, int | None]:
        with self.lock, self.storage.transaction() as connection:
            row = connection.execute(select(_sessions).where(_sessions.c.name == self.name)).one()
            if row.pending or row.credentials is None:
                return None, None
            return self.adapter.validate_json(row.credentials), row.generation

    def is_pending(self) -> bool:
        with self.lock, self.storage.transaction() as connection:
            return bool(
                connection.execute(select(_sessions.c.pending).where(_sessions.c.name == self.name)).scalar_one()
            )

    def begin_operation(self, *, replace_pending: bool = False) -> None:
        """Fence credentials before dispatch; only a fresh import may replace uncertainty."""
        with self.lock, self.storage.transaction() as connection:
            statement = update(_sessions).where(_sessions.c.name == self.name)
            if not replace_pending:
                statement = statement.where(_sessions.c.pending.is_(False))
            result = connection.execute(statement.values(pending=True, credentials=None))
            if result.rowcount != 1:
                raise ValueError("Session operation is already pending; fresh sign-in required")

    def save(self, candidate: T) -> None:
        """Commit verified credentials and clear pending in one durable transaction."""
        credentials = self.adapter.dump_json(candidate)
        with self.lock, self.storage.transaction() as connection:
            previous = connection.execute(
                select(_sessions.c.generation).where(_sessions.c.name == self.name)
            ).scalar_one()
            connection.execute(
                update(_sessions)
                .where(_sessions.c.name == self.name)
                .values(credentials=credentials, pending=False, generation=max(time.time_ns(), (previous or 0) + 1))
            )

    def discard(self) -> None:
        self.begin_operation(replace_pending=True)
