# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
import json
from pathlib import Path
from typing import Any

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import SessionRow
from x.ai_session_archive.ingest import CLAUDE_TITLE_PROJECTION_VERSION, ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_claude_title_projection_follows_native_renames(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, environment = _new_repository(tmp_path)
    home = Path(environment["HOME"])
    path = home / ".claude/projects/example-project/example-session.jsonl"
    path.parent.mkdir(parents=True)
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "<local-command-caveat>Local command</local-command-caveat>"},
        },
        {"type": "user", "message": {"content": "Inspect the parser"}},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    _backup(environment, Path(".claude"), cwd=home)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("example-host", "Example host", repository, (HarnessRoot(Harness.CLAUDE, "/.claude"),))
    )
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert db.scalar(select(SessionRow.title)) == "Inspect the parser"

    for name in ("Custom session name", "Renamed session"):
        records.append({"type": "custom-title", "customTitle": name, "sessionId": "example-session"})
        records.append({"type": "summary", "summary": "Generated summary", "leafUuid": "example-leaf"})
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        _backup(environment, Path(".claude"), cwd=home)
        assert ingestor.ingest().snapshots_incomplete == 0
        with factory() as db:
            assert db.scalar(select(SessionRow.title)) == name


def test_claude_title_projection_backfills_stored_ai_title_records(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, environment = _new_repository(tmp_path)
    home = Path(environment["HOME"])
    path = home / ".claude/projects/example-project/ff64fbf0-73ff-46fb-af69-3749e514879f.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "user", "message": {"content": "Inspect the parser"}},
        {
            "type": "ai-title",
            "aiTitle": "Verify-docs skill conversion",
            "sessionId": "ff64fbf0-73ff-46fb-af69-3749e514879f",
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    _backup(environment, Path(".claude"), cwd=home)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("example-host", "Example host", repository, (HarnessRoot(Harness.CLAUDE, "/.claude"),))
    )
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db, db.begin():
        session = db.scalar(select(SessionRow))
        assert session is not None
        assert session.title == "Verify-docs skill conversion"
        session.title = "Inspect the parser"
        session.claude_title_projection_version = None

    # This second run sees an already complete snapshot, so the projection refresh must
    # read the durable record payloads instead of relying on a transcript reparse.
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        session = db.scalar(select(SessionRow))
        assert session is not None
        assert session.title == "Verify-docs skill conversion"
        assert session.claude_title_projection_version == CLAUDE_TITLE_PROJECTION_VERSION


if __name__ == "__main__":
    pytest_bazel.main()
