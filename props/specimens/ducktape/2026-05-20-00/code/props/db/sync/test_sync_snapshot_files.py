"""Tests for sync_snapshot_files_to_db."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Generator

import pytest
import pytest_bazel
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.splits import Split
from props.db.database import Database
from props.db.models import Snapshot, SnapshotFile
from props.db.sync.sync import sync_snapshot_files_to_db

SLUG = SnapshotSlug("test-sync-files/train1")


def _make_tar(*entries: tuple[str, bytes]) -> bytes:
    """Build an in-memory uncompressed tar from (name, content) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
def session(db: Database) -> Generator[Session]:
    with db.session() as s:
        # Snapshot row required by SnapshotFile FK.
        s.add(Snapshot(slug=SLUG, split=Split.TRAIN, content=b""))
        s.flush()
        yield s
        s.rollback()


def test_non_utf8_files_are_skipped(session: Session):
    """Non-UTF-8 files must not appear in snapshot_files; UTF-8 files must be indexed."""
    archive = _make_tar(("utf8.py", b"hello\nworld\n"), ("binary.bin", b"\xff\xfe binary garbage \x00\x01"))
    sync_snapshot_files_to_db(session, SLUG, archive)

    indexed = {r.file_path for r in session.query(SnapshotFile).filter_by(snapshot_slug=SLUG).all()}
    assert indexed == {"utf8.py"}


def test_utf8_line_count(session: Session):
    """Line count is computed correctly for a UTF-8 file."""
    archive = _make_tar(("lines.py", b"a\nb\nc"))  # 3 lines, no trailing newline
    sync_snapshot_files_to_db(session, SLUG, archive)

    row = session.query(SnapshotFile).filter_by(snapshot_slug=SLUG, file_path="lines.py").one()
    assert row.line_count == 3


if __name__ == "__main__":
    pytest_bazel.main()
