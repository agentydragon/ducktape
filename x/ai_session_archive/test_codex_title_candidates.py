"""Incremental candidates retain the full-history projection across retries and bootstrap."""

# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import Engine, event, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from util.testing.postgres import DatabaseNames, create_temporary_database
from x.ai_session_archive.codex_title_projection import backfill_codex_title_candidates_batch
from x.ai_session_archive.database import (
    CodexTitleBackfillRow,
    CodexTitleCandidateRow,
    CodexTitleObservationRow,
    MachineRow,
    ResticSnapshotRow,
    SessionRow,
    make_engine,
)
from x.ai_session_archive.database_migrate import RUNNER
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticRepository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]
START = datetime(2026, 1, 1, tzinfo=UTC)
PATH = "/.codex/sessions/test.jsonl"


def _seed(factory: Callable[[], Session], count: int) -> None:
    with factory() as db, db.begin():
        db.add(MachineRow(id="host", display_name="Host"))
        db.flush()
        db.add(
            SessionRow(
                id=uuid4(),
                machine_id="host",
                harness="codex",
                source_path=PATH,
                source_relative_path="test.jsonl",
                native_session_id="test",
                native_session_id_inferred=False,
            )
        )
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


def _ingestor(factory: Callable[[], Session]) -> SnapshotIngestor:
    return SnapshotIngestor(
        factory,
        ArchiveSource(
            "host", "Host", ResticRepository(binary="unused", environment={}), (HarnessRoot(Harness.CODEX, "/.codex"),)
        ),
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


@pytest.fixture
def legacy_factory(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[sessionmaker[Session]]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        RUNNER.apply(url, revision="0004_claude_title_projection")
        engine = make_engine(url)
        try:
            yield sessionmaker(engine, expire_on_commit=False)
        finally:
            engine.dispose()


def test_bounded_backfill_and_steady_import_use_compact_candidates(legacy_factory: sessionmaker[Session]) -> None:
    # More than two production-size pages, with history absent from Restic.
    _seed(legacy_factory, 4100)
    engine = legacy_factory.kw["bind"]
    RUNNER.apply(engine.url.render_as_string(hide_password=False))
    assert not backfill_codex_title_candidates_batch(legacy_factory, "host")
    with legacy_factory() as db:
        state = db.get(CodexTitleBackfillRow, "host")
        assert state is not None
        assert not state.complete
        assert state.snapshot_id == f"{4099:064x}"
        assert not state.latest_snapshot_seeded
    # Resume through the public import path after an interrupted bootstrap.
    ingestor = _ingestor(legacy_factory)
    with patch.object(ingestor._source.restic, "list_snapshots", return_value=[]):
        ingestor.ingest()
    assert _candidates(legacy_factory) == _reference(legacy_factory)
    with legacy_factory() as db:
        assert db.scalar(select(CodexTitleBackfillRow.complete)) is True
        row = db.scalar(select(SessionRow))
        assert row is not None
        assert (row.title, row.session_kind) == ("Name 4098", "main")

    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with patch.object(ingestor._source.restic, "list_snapshots", return_value=[]):
            ingestor.ingest()
        with legacy_factory() as db, db.begin():
            row = db.scalar(select(SessionRow))
            assert row is not None
            ingestor._apply_latest_codex_title(db, row)
            assert (row.title, row.session_kind) == ("Name 4098", "main")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements
    assert all("codex_title_observations" not in statement for statement in statements)


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
        row = db.get(CodexTitleObservationRow, ("host", f"{3:064x}", "test"))
        assert row is not None
        db.delete(row)
    assert _candidates(factory) == _reference(factory)


@pytest.mark.parametrize("mutation", ["update", "delete", "insert"])
def test_backfill_rereads_after_concurrent_legacy_write(legacy_factory: sessionmaker[Session], mutation: str) -> None:
    _seed(legacy_factory, 3)
    engine = legacy_factory.kw["bind"]
    RUNNER.apply(engine.url.render_as_string(hide_password=False))
    assert not backfill_codex_title_candidates_batch(legacy_factory, "host", batch_size=1)
    changed = False

    def mutate(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        nonlocal changed
        if changed or not statement.startswith("SELECT archive_lock_codex_title"):
            return
        changed = True
        with legacy_factory() as db, db.begin():
            if mutation == "insert":
                # This key is behind the committed cursor and won't be backfilled.
                db.add(
                    CodexTitleObservationRow(
                        machine_id="host",
                        snapshot_id=f"{0:064x}",
                        native_session_id="earlier-id",
                        source_path="earlier-path",
                        title="Inserted behind cursor",
                    )
                )
            else:
                row = db.get(CodexTitleObservationRow, ("host", f"{2:064x}", "test"))
                assert row is not None
                if mutation == "delete":
                    db.delete(row)
                else:
                    row.source_path = "moved-path"
                    row.title = "Changed during bootstrap"

    event.listen(engine, "before_cursor_execute", mutate)
    try:
        while not backfill_codex_title_candidates_batch(legacy_factory, "host", batch_size=2):
            pass
    finally:
        event.remove(engine, "before_cursor_execute", mutate)
    assert changed
    assert _candidates(legacy_factory) == _reference(legacy_factory)


def test_backfill_can_read_while_legacy_writer_holds_an_observation_row(legacy_factory: sessionmaker[Session]) -> None:
    _seed(legacy_factory, 3)
    engine = legacy_factory.kw["bind"]
    RUNNER.apply(engine.url.render_as_string(hide_password=False))
    row_locked = Event()
    started_writer = False

    def writer() -> None:
        with legacy_factory() as db, db.begin():
            assert db.get(ResticSnapshotRow, ("host", f"{2:064x}"), with_for_update=True) is not None
            row = db.get(CodexTitleObservationRow, ("host", f"{2:064x}", "test"), with_for_update=True)
            assert row is not None
            row_locked.set()
            row.source_path = "changed-while-backfill-has-machine-lock"
            row.title = "Retry after bootstrap"
            db.flush()  # Its AFTER trigger waits for backfill's machine lock.

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []

        def before_read(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
            nonlocal started_writer
            if started_writer or "JOIN restic_snapshots" not in statement:
                return
            started_writer = True
            futures.append(pool.submit(writer))
            assert row_locked.wait(timeout=10)

        event.listen(engine, "before_cursor_execute", before_read)
        try:
            assert not backfill_codex_title_candidates_batch(legacy_factory, "host")
        finally:
            event.remove(engine, "before_cursor_execute", before_read)
        assert started_writer
        for future in futures:
            future.result(timeout=10)
    assert _candidates(legacy_factory) == _reference(legacy_factory)


def test_backfill_crash_rolls_back_candidates_and_cursor(legacy_factory: sessionmaker[Session]) -> None:
    _seed(legacy_factory, 3)
    engine = legacy_factory.kw["bind"]
    RUNNER.apply(engine.url.render_as_string(hide_password=False))

    def fail(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        if statement.startswith("INSERT INTO codex_title_candidates"):
            raise RuntimeError("simulated process failure before commit")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            backfill_codex_title_candidates_batch(legacy_factory, "host")
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with legacy_factory() as db:
        assert db.get(CodexTitleBackfillRow, "host") is None
    assert _candidates(legacy_factory) == {}
    while not backfill_codex_title_candidates_batch(legacy_factory, "host"):
        pass
    assert _candidates(legacy_factory) == _reference(legacy_factory)


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
