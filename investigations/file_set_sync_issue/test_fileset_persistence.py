"""Debug test to check if FileSets persist after fixture sync."""

import pytest_bazel

from props.db.database import Database
from props.db.models import FileSet, FileSetMember


def test_fileset_exists_after_sync(synced_db: Database):
    """Check if d5673969 FileSet exists after fixture sync."""
    with synced_db.session() as session:
        # Query for the problematic FileSet
        fs = (
            session.query(FileSet)
            .filter_by(snapshot_slug="test-fixtures/train1", files_hash="d5673969af8b94a23a229e9215d473c4")
            .first()
        )

        # Check if ANY FileSets exist first
        all_fs = session.query(FileSet).filter_by(snapshot_slug="test-fixtures/train1").all()
        assert len(all_fs) > 0, "NO FileSets found for test-fixtures/train1 after fixture sync!"

        # Now check for the specific FileSet
        assert fs is not None, (
            f"FileSet d5673969af8b94a23a229e9215d473c4 NOT FOUND after fixture sync! "
            f"Found {len(all_fs)} FileSets: {[f.files_hash for f in all_fs]}"
        )

        # Query members
        members = (
            session.query(FileSetMember)
            .filter_by(snapshot_slug="test-fixtures/train1", files_hash="d5673969af8b94a23a229e9215d473c4")
            .all()
        )

        assert len(members) > 0, "FileSet exists but has ZERO members!"
        assert members[0].file_path == "add.py", f"Expected add.py, got {members[0].file_path}"


if __name__ == "__main__":
    pytest_bazel.main()
