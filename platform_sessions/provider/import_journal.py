"""Durable reservations and sanitized results for provider session imports.

Only one provider process may own a journal directory at a time. Reservations are
committed before dispatch; a reservation left by a previous process is unknown,
not eligible for dispatch again. Candidate material is never passed to this module.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from atomicwrites import atomic_write
from filelock import FileLock
from sqlalchemy import Column, LargeBinary, MetaData, String, Table, create_engine, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import URL, Connection
from sqlalchemy.pool import NullPool

from platform_sessions.provider import session_provider_pb2 as pb, validation


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
        missing: list[Path] = []
        ancestor = directory
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        # atomicwrites syncs the containing directory, making each new directory
        # entry durable before an import may be dispatched.
        for path in reversed(missing or [directory]):
            marker = path.parent / f".{path.name}.platform-sessions-journal"
            with atomic_write(marker, overwrite=True) as stream:
                stream.write("")
        self._owner_lock = FileLock(directory / ".import-journal.lock", timeout=0, thread_local=False)
        self._owner_lock.acquire()
        try:
            path = directory / "import-operations.sqlite3"
            self._engine = create_engine(URL.create("sqlite", database=str(path)), poolclass=NullPool)
            with self._transaction() as connection:
                _metadata.create_all(connection)
                # A previous owner may have stopped at any point after dispatch.
                # Never make its identifier eligible for dispatch again.
                connection.execute(
                    update(_imports).where(_imports.c.state == ImportState.PENDING).values(state=ImportState.UNKNOWN)
                )
            path.chmod(0o600)
        except BaseException:
            if hasattr(self, "_engine"):
                self._engine.dispose()
            self._owner_lock.release()
            raise
        self._mutex = threading.Lock()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self._engine.begin() as connection:
            # In DELETE mode EXTRA also fsyncs the journal directory on commit;
            # FULL can lose the most recent transaction after a power failure.
            # NullPool prevents cross-thread connection reuse.
            mode = connection.exec_driver_sql("PRAGMA journal_mode=DELETE").scalar_one()
            if mode != "delete":
                raise OSError("import journal cannot use durable rollback mode")
            connection.exec_driver_sql("PRAGMA synchronous=EXTRA")
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

    def mark_unknown(self, operation_id: str) -> None:
        """Conservatively record an operation whose result could not be persisted."""
        validate_operation_id(operation_id)
        with self._mutex, self._transaction() as connection:
            connection.execute(
                update(_imports)
                .where(_imports.c.operation_id == operation_id, _imports.c.state == ImportState.PENDING)
                .values(state=ImportState.UNKNOWN)
            )

    def close(self) -> None:
        with self._mutex:
            self._engine.dispose()
            self._owner_lock.release()
