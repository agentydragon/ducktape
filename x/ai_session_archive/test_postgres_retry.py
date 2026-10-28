# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
from pathlib import Path

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import FileVersionRow, RecordPayloadRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_postgres_file_transaction_rolls_back_and_retries(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    claude_root = tmp_path / "home" / ".claude"
    transcript = claude_root / "projects" / "example-project" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    _backup(env, claude_root)
    source = ArchiveSource(
        machine_id="transaction-test",
        display_name="Transaction test host",
        restic=repository,
        roots=(HarnessRoot(Harness.CLAUDE, str(claude_root)),),
    )
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(factory, source)

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE FUNCTION reject_archive_record_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'injected archive record failure';
            END;
            $$
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_archive_record_insert
            BEFORE INSERT ON file_version_records
            FOR EACH ROW EXECUTE FUNCTION reject_archive_record_insert()
            """
        )

    failed_report = ingestor.ingest()
    assert failed_report.files_failed == 1
    assert failed_report.snapshots_incomplete == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 0
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 0

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_archive_record_insert ON file_version_records")
        connection.exec_driver_sql("DROP FUNCTION reject_archive_record_insert()")

    retried_report = ingestor.ingest()
    assert retried_report.files_failed == 0
    assert retried_report.snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(FileVersionRow)) == 1
        assert db.scalar(select(func.count()).select_from(RecordPayloadRow)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
