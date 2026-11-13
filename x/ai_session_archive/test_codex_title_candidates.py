"""Incremental candidates follow observation inserts, retries, and deletes."""

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_bazel
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from x.ai_session_archive.database import (
    CodexTitleCandidateRow,
    CodexTitleObservationRow,
    MachineRow,
    ResticSnapshotRow,
)

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]
START = datetime(2026, 1, 1, tzinfo=UTC)
PATH = "/.codex/sessions/test.jsonl"


def _seed(factory: Callable[[], Session], count: int) -> None:
    with factory() as db, db.begin():
        db.add(MachineRow(id="host", display_name="Host"))
        db.flush()
        db.execute(
            insert(ResticSnapshotRow),
            [
                {
                    "machine_id": "host",
                    "snapshot_id": f"{n:064x}",
                    "captured_at": START + timedelta(minutes=n),
                    "paths": [],
                }
                for n in range(count)
            ],
        )
        db.execute(
            insert(CodexTitleObservationRow),
            [
                {
                    "machine_id": "host",
                    "snapshot_id": f"{n:064x}",
                    "native_session_id": "test",
                    "source_path": PATH,
                    "title": f"Name {n}" if n % 2 == 0 else None,
                    "session_kind": "main" if n % 2 else None,
                }
                for n in range(count)
            ],
        )


def _candidates(factory: Callable[[], Session]) -> dict[tuple[str, str], tuple[str, str]]:
    with factory() as db:
        return {(r.source_path, r.field): (r.snapshot_id, r.value) for r in db.scalars(select(CodexTitleCandidateRow))}


def _reference(factory: Callable[[], Session]) -> dict[tuple[str, str], tuple[str, str]]:
    latest: dict[tuple[str, str], tuple[datetime, str, str]] = {}
    with factory() as db:
        for row, captured_at in db.execute(
            select(CodexTitleObservationRow, ResticSnapshotRow.captured_at).join(
                ResticSnapshotRow,
                (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
            )
        ):
            for field, value in (("title", row.title), ("session_kind", row.session_kind)):
                if value is not None:
                    key = (row.source_path, field)
                    rank = (captured_at, row.snapshot_id, value)
                    if key not in latest or rank > latest[key]:
                        latest[key] = rank
    return {key: (rank[1], rank[2]) for key, rank in latest.items()}


def test_retries_retract_fields_move_paths_and_respect_capture_rank(postgres_engine: Engine) -> None:
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(factory, 4)
    # Retrying the same winning snapshot replaces a lexically greater old value.
    with factory() as db, db.begin():
        row = db.get(CodexTitleObservationRow, ("host", f"{2:064x}", "test"))
        assert row is not None
        row.title = "A replacement"
    assert _candidates(factory) == _reference(factory)
    # Title and kind winners differ. Null retracts title only, then path moves
    # retract the former mapping and establish an independent index-only candidate.
    with factory() as db, db.begin():
        row = db.get(CodexTitleObservationRow, ("host", f"{2:064x}", "test"))
        assert row is not None
        row.title = None
    assert _candidates(factory) == _reference(factory)
    with factory() as db, db.begin():
        row = db.get(CodexTitleObservationRow, ("host", f"{3:064x}", "test"))
        assert row is not None
        row.source_path = ""
        row.title = "Index capture"
    assert _candidates(factory) == _reference(factory)
    # A historical capture arriving late cannot outrank a newer capture merely
    # because its snapshot ID is greater. Equal times use snapshot ID ordering.
    with factory() as db, db.begin():
        db.add(ResticSnapshotRow(machine_id="host", snapshot_id="f" * 64, captured_at=START, paths=[]))
        db.flush()
        db.add(
            CodexTitleObservationRow(
                machine_id="host",
                snapshot_id="f" * 64,
                native_session_id="test",
                source_path="",
                title="Late old capture",
                session_kind="guardian",
            )
        )
    assert _candidates(factory) == _reference(factory)
    with factory() as db, db.begin():
        db.add(
            ResticSnapshotRow(
                machine_id="host", snapshot_id="e" * 64, captured_at=START + timedelta(minutes=3), paths=[]
            )
        )
        db.flush()
        db.add(
            CodexTitleObservationRow(
                machine_id="host",
                snapshot_id="e" * 64,
                native_session_id="test",
                source_path="",
                title="Equal-time capture",
            )
        )
    assert _candidates(factory)[("", "title")] == ("e" * 64, "Equal-time capture")
    assert _candidates(factory) == _reference(factory)
    with factory() as db, db.begin():
        tied = db.get(CodexTitleObservationRow, ("host", "e" * 64, "test"))
        assert tied is not None
        db.delete(tied)
    assert _candidates(factory) == _reference(factory)
    with factory() as db, db.begin():
        row = db.get(CodexTitleObservationRow, ("host", f"{3:064x}", "test"))
        assert row is not None
        db.delete(row)
    assert _candidates(factory) == _reference(factory)


def test_snapshot_batch_uses_one_advisory_lock(postgres_engine: Engine) -> None:
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(factory, 1)
    with factory() as db, db.begin():
        db.execute(
            insert(CodexTitleObservationRow),
            [
                {
                    "machine_id": "host",
                    "snapshot_id": f"{0:064x}",
                    "native_session_id": f"session-{n}",
                    "source_path": f"/.codex/sessions/session-{n}.jsonl",
                    "title": f"Session {n}",
                }
                for n in range(200)
            ],
        )
        assert (
            db.scalar(text("SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'")) == 1
        )


def test_candidates_accept_long_physical_paths(postgres_engine: Engine) -> None:
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(factory, 1)
    # Incompressible enough to exceed a btree tuple's limit when indexed in full.
    long_path = "/" + "/".join(str(uuid4()) for _ in range(95))
    with factory() as db, db.begin():
        row = db.get(CodexTitleObservationRow, ("host", f"{0:064x}", "test"))
        assert row is not None
        row.source_path = long_path
    assert _candidates(factory) == {(long_path, "title"): (f"{0:064x}", "Name 0")}


if __name__ == "__main__":
    pytest_bazel.main()
