"""SQLite-backed single-row state store.

The app's state is a single opaque JSON blob keyed nowhere (singleton). We store
it alongside a content-addressed ETag so the frontend can use If-Match to avoid
clobbering concurrent edits from another device.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StateRecord:
    blob: bytes
    etag: str


def _compute_etag(blob: bytes) -> str:
    # Weak ETag semantics are fine — the blob is already the state, so any
    # byte-identical blob is interchangeable. Using sha256 of the body.
    return f'"{hashlib.sha256(blob).hexdigest()[:16]}"'


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  blob BLOB NOT NULL,"
                "  etag TEXT NOT NULL"
                ")"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def load(self) -> StateRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT blob, etag FROM state WHERE id = 1").fetchone()
        if row is None:
            return None
        return StateRecord(blob=row[0], etag=row[1])

    def save(self, blob: bytes) -> StateRecord:
        etag = _compute_etag(blob)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state (id, blob, etag) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET blob = excluded.blob, etag = excluded.etag",
                (blob, etag),
            )
        return StateRecord(blob=blob, etag=etag)
