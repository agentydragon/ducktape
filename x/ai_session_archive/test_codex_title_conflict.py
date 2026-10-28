# gazelle:include_dep //util/testing:postgres_fixtures
# gazelle:include_dep :testing
"""An index-only Codex title cannot identify one of two physical transcripts."""

import json
from pathlib import Path

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from x.ai_session_archive.database import SessionRow
from x.ai_session_archive.ingest import ArchiveSource, HarnessRoot, SnapshotIngestor
from x.ai_session_archive.models import Harness
from x.ai_session_archive.testing import _backup, _new_repository

pytest_plugins = ["util.testing.postgres_fixtures", "x.ai_session_archive.testing"]


def test_index_title_requires_unique_native_identity(tmp_path: Path, postgres_engine: Engine) -> None:
    repository, env = _new_repository(tmp_path)
    home = Path(env["HOME"])
    codex = home / ".codex"
    for relative in ("sessions/2026/01/shared.jsonl", "archived_sessions/shared.jsonl"):
        transcript = codex / relative
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"type":"session_meta","payload":{"id":"shared"}}\n')
    (codex / "session_index.jsonl").write_text(
        json.dumps({"id": "shared", "thread_name": "Ambiguous", "updated_at": "2026-01-01T00:00:00Z"}) + "\n"
    )
    _backup(env, Path(".codex"), cwd=home)
    factory = sessionmaker(postgres_engine, expire_on_commit=False)
    ingestor = SnapshotIngestor(
        factory, ArchiveSource("host", "Host", repository, (HarnessRoot(Harness.CODEX, "/.codex"),))
    )
    assert ingestor.ingest().snapshots_incomplete == 0
    with factory() as db:
        assert [row.title for row in db.scalars(select(SessionRow))] == [None, None]


if __name__ == "__main__":
    pytest_bazel.main()
