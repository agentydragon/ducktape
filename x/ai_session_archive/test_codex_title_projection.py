# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""Title projection preserves physical-file ambiguity and index-only gating."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_bazel
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import CodexTitleObservationRow, MachineRow, ResticSnapshotRow, SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.restic import ResticRepository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_projection_preserves_paths_and_index_only_ambiguity(postgres_engine: Engine) -> None:
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    paths = ("/.codex/sessions/first.jsonl", "/.codex/sessions/duplicate.jsonl")
    session_ids = (uuid4(), uuid4())
    index_session = uuid4()
    with factory() as db, db.begin():
        db.add(MachineRow(id="host", display_name="Host"))
        db.flush()
        for session_id, path in zip(session_ids, paths, strict=True):
            db.add(
                SessionRow(
                    id=session_id,
                    machine_id="host",
                    harness="codex",
                    source_path=path,
                    source_relative_path=path,
                    native_session_id="duplicate-native-id",
                    native_session_id_inferred=False,
                )
            )
        db.add(
            SessionRow(
                id=index_session,
                machine_id="host",
                harness="codex",
                source_path="/.codex/sessions/index-only.jsonl",
                source_relative_path="index-only.jsonl",
                native_session_id="index-only",
                native_session_id_inferred=False,
                title="Previous captured name",
                codex_title_from_metadata=True,
            )
        )
        for number in range(6):
            snapshot_id = f"{number:064x}"
            db.add(
                ResticSnapshotRow(
                    machine_id="host",
                    snapshot_id=snapshot_id,
                    captured_at=started + timedelta(minutes=number),
                    paths=[],
                )
            )
            db.flush()
            db.add(
                CodexTitleObservationRow(
                    machine_id="host",
                    snapshot_id=snapshot_id,
                    native_session_id="duplicate-native-id",
                    source_path=paths[number % 2] if number < 4 else "",
                    title=f"Name {number}",
                    title_source="sqlite_name" if number < 4 else "session_index",
                )
            )
            db.add(
                CodexTitleObservationRow(
                    machine_id="host",
                    snapshot_id=snapshot_id,
                    native_session_id="index-only",
                    source_path="",
                    title=f"Index name {number}",
                    title_source="session_index",
                )
            )

    ingestor = SnapshotIngestor(
        factory,
        ArchiveSource(
            "host", "Host", ResticRepository(binary="unused", environment={}), (HarnessRoot(Harness.CODEX, "/.codex"),)
        ),
    )
    ingestor._project_codex_titles(allow_index_only=False)
    with factory() as db:
        first, duplicate, indexed = (db.get(SessionRow, key) for key in (*session_ids, index_session))
        assert first is not None
        assert first.title == "Name 2"
        assert duplicate is not None
        assert duplicate.title == "Name 3"
        assert indexed is not None
        assert indexed.title == "Previous captured name"

    ingestor._project_codex_titles(allow_index_only=True)
    with factory() as db:
        first, duplicate, indexed = (db.get(SessionRow, key) for key in (*session_ids, index_session))
        # A newer index-only observation cannot disambiguate two physical files.
        assert first is not None
        assert first.title == "Name 2"
        assert duplicate is not None
        assert duplicate.title == "Name 3"
        assert indexed is not None
        assert indexed.title == "Index name 5"


if __name__ == "__main__":
    pytest_bazel.main()
