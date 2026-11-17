"""Durable reservations and sanitized results for provider session imports.

Only one provider process may own a journal directory at a time. Reservations are
committed before dispatch; a reservation left by a previous process is unknown,
not eligible for dispatch again. Candidate material is never passed to this module.
"""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from sqlalchemy import Column, LargeBinary, MetaData, String, Table, create_engine, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL, Connection
from sqlalchemy.pool import NullPool

from platform_sessions.provider.v1 import session_provider_pb2 as pb, validation


class ImportState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImportRecord:
    state: ImportState
    result: pb.ImportSessionResponse | None = None


_metadata = MetaData()
_imports = Table(
    "imports",
    _metadata,
    Column("operation_id", String, primary_key=True),
    Column("state", String, nullable=False),
    Column("result", LargeBinary),
)


def validate_operation_id(operation_id: str) -> None:
    """Require a canonical random UUID, suitable for use as an opaque operation ID."""
    try:
        value = UUID(operation_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid import operation ID") from exc
    if value.version != 4 or str(value) != operation_id:
        raise ValueError("invalid import operation ID")


class ImportJournal:
    """A non-evicting import journal owned exclusively by one provider process."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_fd = os.open(directory / ".import-journal.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = directory / "import-operations.sqlite3"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            os.close(fd)
            self._engine = create_engine(URL.create("sqlite", database=str(path)), poolclass=NullPool)
            with self._transaction() as connection:
                _metadata.create_all(connection)
                # A previous owner may have stopped at any point after dispatch.
                # Never make its identifier eligible for dispatch again.
                connection.execute(
                    update(_imports).where(_imports.c.state == ImportState.PENDING).values(state=ImportState.UNKNOWN)
                )
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if hasattr(self, "_engine"):
                self._engine.dispose()
            os.close(self._lock_fd)
            raise
        self._mutex = threading.Lock()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self._engine.begin() as connection:
            # Each connection must fsync the rollback journal and database before
            # returning success. NullPool prevents cross-thread connection reuse.
            connection.exec_driver_sql("PRAGMA synchronous=FULL")
            yield connection

    def reserve(self, operation_id: str) -> tuple[bool, ImportRecord]:
        """Atomically reserve an ID; existing IDs are returned without redispatch."""
        validate_operation_id(operation_id)
        with self._mutex, self._transaction() as connection:
            cursor = connection.execute(
                insert(_imports)
                .values(operation_id=operation_id, state=ImportState.PENDING)
                .on_conflict_do_nothing(index_elements=[_imports.c.operation_id])
            )
            record = self._get(connection, operation_id)
            assert record is not None
            return cursor.rowcount == 1, record

    def get(self, operation_id: str) -> ImportRecord | None:
        validate_operation_id(operation_id)
        with self._mutex, self._transaction() as connection:
            return self._get(connection, operation_id)

    def _get(self, connection: Connection, operation_id: str) -> ImportRecord | None:
        row = connection.execute(
            select(_imports.c.state, _imports.c.result).where(_imports.c.operation_id == operation_id)
        ).one_or_none()
        if row is None:
            return None
        state = ImportState(row.state)
        if state is ImportState.COMPLETED:
            if row.result is None:
                raise ValueError("invalid import journal record")
            result = pb.ImportSessionResponse.FromString(row.result)
            validation.candidate_result(result)
            return ImportRecord(state, result)
        if row.result is not None:
            raise ValueError("invalid import journal record")
        return ImportRecord(state)

    def complete(self, operation_id: str, result: pb.ImportSessionResponse) -> None:
        validate_operation_id(operation_id)
        # Unknown protobuf fields are untrusted bytes, and the provider-supplied
        # instance is unnecessary. Neither belongs in the durable record.
        sanitized = pb.ImportSessionResponse()
        sanitized.CopyFrom(result)
        sanitized.ClearField("instance")
        sanitized.DiscardUnknownFields()
        validation.candidate_result(sanitized)
        with self._mutex, self._transaction() as connection:
            cursor = connection.execute(
                update(_imports)
                .where(_imports.c.operation_id == operation_id, _imports.c.state == ImportState.PENDING)
                .values(state=ImportState.COMPLETED, result=sanitized.SerializeToString())
            )
            if cursor.rowcount != 1:
                raise ValueError("import reservation is not pending")

    def close(self) -> None:
        with self._mutex:
            self._engine.dispose()
            os.close(self._lock_fd)
