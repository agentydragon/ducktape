"""Durable reservations and sanitized results for provider session imports.

Only one provider process may own a journal directory at a time. Reservations are
committed before dispatch; a reservation left by a previous process is unknown,
not eligible for dispatch again. Candidate material is never passed to this module.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from platform_sessions.provider.v1 import session_provider_pb2 as pb, validation


class ImportState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImportRecord:
    state: ImportState
    result: pb.ImportSessionResponse | None = None


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
            self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS imports (operation_id TEXT PRIMARY KEY, state TEXT NOT NULL, result BLOB)"
            )
            # A previous owner may have stopped at any point after dispatch. Never
            # infer failure or make its identifier eligible for dispatch again.
            self._db.execute("UPDATE imports SET state = ? WHERE state = ?", (ImportState.UNKNOWN, ImportState.PENDING))
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if hasattr(self, "_db"):
                self._db.close()
            os.close(self._lock_fd)
            raise
        self._mutex = threading.Lock()

    def reserve(self, operation_id: str) -> tuple[bool, ImportRecord]:
        """Atomically reserve an ID; existing IDs are returned without redispatch."""
        validate_operation_id(operation_id)
        with self._mutex:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO imports (operation_id, state) VALUES (?, ?)", (operation_id, ImportState.PENDING)
            )
            record = self._get(operation_id)
            assert record is not None
            return cursor.rowcount == 1, record

    def get(self, operation_id: str) -> ImportRecord | None:
        validate_operation_id(operation_id)
        with self._mutex:
            return self._get(operation_id)

    def _get(self, operation_id: str) -> ImportRecord | None:
        row = self._db.execute("SELECT state, result FROM imports WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            return None
        state = ImportState(row[0])
        if state is ImportState.COMPLETED:
            if row[1] is None:
                raise ValueError("invalid import journal record")
            result = pb.ImportSessionResponse.FromString(row[1])
            validation.candidate_result(result)
            return ImportRecord(state, result)
        if row[1] is not None:
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
        with self._mutex:
            cursor = self._db.execute(
                "UPDATE imports SET state = ?, result = ? WHERE operation_id = ? AND state = ?",
                (ImportState.COMPLETED, sanitized.SerializeToString(), operation_id, ImportState.PENDING),
            )
            if cursor.rowcount != 1:
                raise ValueError("import reservation is not pending")

    def close(self) -> None:
        with self._mutex:
            self._db.close()
            os.close(self._lock_fd)
