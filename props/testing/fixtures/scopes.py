"""Scope fixtures (ExampleSpec variants) for props tests."""

import pytest

from props.core.ids import SnapshotSlug
from props.core.models.examples import SingleFileSetExample, WholeSnapshotExample
from props.db.database import Database
from props.db.models import FileSet, FileSetMember


@pytest.fixture
def all_files_scope(test_snapshot: SnapshotSlug) -> WholeSnapshotExample:
    """WholeSnapshotExample for test-trivial fixture."""
    return WholeSnapshotExample(snapshot_slug=test_snapshot)


@pytest.fixture
def subtract_file_example(synced_db: Database) -> SingleFileSetExample:
    """SingleFileSetExample for subtract.py in train1."""
    slug = SnapshotSlug("test-fixtures/train1")
    with synced_db.session() as session:
        fs = (
            session.query(FileSet)
            .join(FileSetMember)
            .filter(FileSet.snapshot_slug == slug)
            .filter(FileSetMember.file_path == "subtract.py")
            .first()
        )
        assert fs is not None, "No file set found for subtract.py in train1"
        return SingleFileSetExample(snapshot_slug=slug, files_hash=fs.files_hash)
