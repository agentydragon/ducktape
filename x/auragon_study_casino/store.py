"""Server-authoritative DocStore for the casino's Y.Doc.

The store holds one Y.Doc in memory and persists it as a single binary
update blob in SQLite. Every `POST /sync` request goes through
`apply_client_update`, which:

1. Builds a *trial* doc by cloning the canonical state and applying
   the inbound client update on top of it.
2. Runs every validator from `validators.py` against the trial.
3. On success, promotes the trial to canonical, persists, and returns
   the binary diff the client doesn't yet have.
4. On failure, the canonical doc is unchanged and the caller gets a
   `Rejected` describing which rule was violated.

There is no event log: pycrdt's binary update format already encodes
every op (Yjs-style CRDT operations are themselves the history). The
SyncStatus rejection contract on the client mirrors the structure
returned here, so the UI can roll back the offending transaction via
`Y.UndoManager`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.auragon_study_casino.doc_shape import Casino
from x.auragon_study_casino.models import Base, DocRow
from x.auragon_study_casino.validators import ValidationError, validate


@dataclass(frozen=True)
class Accepted:
    """The client's update was applied and persisted."""

    server_update: bytes
    """Binary update the client should apply to catch up to the server's
    current state, computed against the state vector the client sent."""

    server_state_vector: bytes
    """Server's state vector after the merge — the client should remember
    this and pass it on the next sync as `since_state_vector`."""


@dataclass(frozen=True)
class Rejected:
    """The client's update would have violated a business rule."""

    rule: str
    message: str


class DocStore:
    """Owns the canonical Y.Doc and gates writes through the validators."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        with self._engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.commit()
        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)

        # Lock around the canonical doc + persistence step. pycrdt is not
        # thread-safe and FastAPI may serve requests from multiple threads;
        # the critical section (clone → apply → validate → persist) needs
        # to be atomic.
        self._lock = RLock()

        # Seed an empty canonical doc on first boot.
        with self._Session() as s:
            row = s.scalar(select(DocRow).where(DocRow.id == 1))
            if row is None:
                seed = Casino.empty()
                s.add(DocRow(id=1, update_blob=seed.get_update()))
                s.commit()
                self._canonical = seed
            else:
                self._canonical = Casino.from_update(row.update_blob)

    @property
    def canonical(self) -> Casino:
        """Read-only access to the canonical doc; do not mutate."""
        return self._canonical

    def get_update_for_client(self, client_state_vector: bytes | None) -> bytes:
        """Binary update the client needs to catch up to the server's view."""
        with self._lock:
            return self._canonical.get_update(client_state_vector)

    def get_server_state_vector(self) -> bytes:
        with self._lock:
            return self._canonical.get_state()

    def snapshot_for_client(self, client_state_vector: bytes | None) -> tuple[bytes, bytes]:
        """Return (update_for_client, server_state_vector) atomically.

        Two callers (`/sync`'s pure-pull path and the bootstrap path) want a
        binary update and the matching server state vector. Calling
        `get_update_for_client` and `get_server_state_vector` separately is
        racy: another thread can promote a new canonical between the two
        unlocked sections, leaving the client with an update from version V
        and a state vector from version V+1. Generate the pair under a
        single lock acquisition.
        """
        with self._lock:
            return (self._canonical.get_update(client_state_vector), self._canonical.get_state())

    def apply_client_update(self, client_update: bytes, client_state_vector: bytes) -> Accepted | Rejected:
        """Apply `client_update` to a trial Casino, validate, persist on success.

        `client_state_vector` is the state vector the client had *before*
        producing this update; we use it to compute the minimal `server_update`
        the client still needs after our merge.

        Failure modes, all reported via `Rejected` so the client can surface a
        toast and roll back rather than seeing a 500:
        - `invalid_update`: pycrdt couldn't decode the client's binary blob
          (corrupt or truncated). Catching here keeps a malformed payload
          from taking the sync surface down.
        - one of the validators in `validators.py` raised.

        Persistence ordering is **persist first, then promote**: the SQLite
        write commits before `_canonical` swaps, so a disk error can't leave
        the in-memory doc ahead of what survives a process restart.
        """
        with self._lock:
            trial = Casino.from_update(self._canonical.get_update())
            try:
                trial.apply_update(client_update)
            except Exception as e:
                return Rejected(rule="invalid_update", message=f"could not decode client update: {e}")

            try:
                validate(trial)
            except ValidationError as e:
                return Rejected(rule=e.rule, message=e.message)

            # Persist first; only swap _canonical after the DB commits so a
            # disk error can't leave memory ahead of the persisted state.
            trial_update = trial.get_update()
            with self._Session() as s, s.begin():
                row = s.scalar(select(DocRow).where(DocRow.id == 1).with_for_update())
                assert row is not None
                row.update_blob = trial_update
            self._canonical = trial

            return Accepted(server_update=trial.get_update(client_state_vector), server_state_vector=trial.get_state())


# Enable WAL on every pooled connection.
@event.listens_for(Engine, "connect")
def _sqlite_pragma_on_connect(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
