"""Minimal reproduction case for FileSetMember persistence bug.

Tests basic FileSet/FileSetMember persistence without the sync workflow complexity.
This helps isolate whether the bug is in:
- Basic ORM persistence (unlikely)
- The sync workflow specifically
- Some interaction with other tables
"""

from __future__ import annotations

import hashlib

import pytest_bazel
from sqlalchemy import select

from props.core.ids import SnapshotSlug
from props.core.splits import Split
from props.db.database import Database
from props.db.models import FileSet, FileSetMember, Snapshot, SnapshotFile


def test_fileset_member_basic_persistence(db: Database):
    """Test that FileSetMember rows persist across sessions.

    This is a minimal reproduction case. We:
    1. Create a Snapshot
    2. Create a SnapshotFile
    3. Create a FileSet with one FileSetMember
    4. Flush and verify member exists in session
    5. Commit the transaction
    6. Open a NEW session and query for the FileSetMember
    7. Assert it exists

    If this fails, the bug is in basic ORM persistence.
    If this passes, the bug is specific to the sync workflow.
    """
    snapshot_slug = SnapshotSlug("test/minimal-repro")
    file_path = "src/test.py"
    files_hash = hashlib.md5(file_path.encode()).hexdigest()

    # Session 1: Create and commit
    with db.session() as session:
        # Create snapshot
        snapshot = Snapshot(slug=snapshot_slug, split=Split.TRAIN)
        session.add(snapshot)

        # Create snapshot file (required for FileSetMember FK)
        snapshot_file = SnapshotFile(snapshot_slug=snapshot_slug, file_path=file_path, line_count=10)
        session.add(snapshot_file)

        # Create fileset
        fileset = FileSet(snapshot_slug=snapshot_slug, files_hash=files_hash)
        session.add(fileset)

        # Create fileset member
        member = FileSetMember(snapshot_slug=snapshot_slug, files_hash=files_hash, file_path=file_path)
        session.add(member)

        # Flush to database
        session.flush()

        # Verify member exists in this session
        stmt = select(FileSetMember).where(
            FileSetMember.snapshot_slug == snapshot_slug,
            FileSetMember.files_hash == files_hash,
            FileSetMember.file_path == file_path,
        )
        result = session.execute(stmt).scalar_one_or_none()
        assert result is not None, "FileSetMember should exist after flush"

        # Commit transaction
        session.commit()

    # Session 2: Query in a fresh session
    with db.session() as session:
        stmt = select(FileSetMember).where(
            FileSetMember.snapshot_slug == snapshot_slug,
            FileSetMember.files_hash == files_hash,
            FileSetMember.file_path == file_path,
        )
        result = session.execute(stmt).scalar_one_or_none()

        # This is where the bug shows up - member exists in session 1 but not session 2
        assert result is not None, (
            f"FileSetMember should persist across sessions. "
            f"Expected member for {snapshot_slug=}, {files_hash=}, {file_path=}"
        )


def test_fileset_member_like_sync_workflow(db: Database):
    """Test FileSetMember persistence using the exact pattern from sync.py.

    This mimics sync_file_sets_to_db (lines 811-818):
    - Create FileSet
    - Flush
    - Add FileSetMembers in a loop
    - No flush after members
    - Commit later

    If this fails, we've found the bug!
    """
    snapshot_slug = SnapshotSlug("test/sync-pattern")
    file_paths = ["src/a.py", "src/b.py", "src/c.py"]
    path_strs = sorted(file_paths)
    files_hash = hashlib.md5("".join(path_strs).encode()).hexdigest()

    # Session 1: Create using sync pattern
    with db.session() as session:
        # Create snapshot
        snapshot = Snapshot(slug=snapshot_slug, split=Split.TRAIN)
        session.add(snapshot)

        # Create snapshot files (required for FileSetMember FK)
        for path in file_paths:
            session.add(SnapshotFile(snapshot_slug=snapshot_slug, file_path=path, line_count=10))

        # THIS IS THE PATTERN FROM sync.py lines 811-818
        fs = FileSet(snapshot_slug=snapshot_slug, files_hash=files_hash)
        session.add(fs)
        session.flush()  # ensure FK for members
        for file_path in file_paths:
            session.add(FileSetMember(snapshot_slug=snapshot_slug, files_hash=files_hash, file_path=file_path))
        # NOTE: No flush after adding members! Just like sync.py

        # Commit
        session.commit()

    # Session 2: Query in a fresh session
    with db.session() as session:
        # Query for all members
        stmt = select(FileSetMember).where(
            FileSetMember.snapshot_slug == snapshot_slug, FileSetMember.files_hash == files_hash
        )
        results = list(session.execute(stmt).scalars())

        # If the bug is in the sync pattern, we'll get 0 members here
        assert len(results) == len(file_paths), (
            f"FileSetMember should persist with sync pattern. Expected {len(file_paths)} members, got {len(results)}"
        )


def test_fileset_member_with_delete_in_same_transaction(db: Database):
    """Test FileSetMember persistence when deleting another FileSet in the same transaction.

    This mimics the real sync scenario where:
    1. We create new FileSets with members
    2. We delete old FileSets (with CASCADE to members) in the same transaction
    3. Commit

    Hypothesis: Maybe the CASCADE delete is somehow affecting the newly added members?
    """
    snapshot_slug = SnapshotSlug("test/delete-pattern")

    # Session 1: Create an old FileSet that will be deleted
    with db.session() as session:
        snapshot = Snapshot(slug=snapshot_slug, split=Split.TRAIN)
        session.add(snapshot)

        # Create files
        for path in ["old_a.py", "old_b.py", "new_a.py", "new_b.py"]:
            session.add(SnapshotFile(snapshot_slug=snapshot_slug, file_path=path, line_count=10))

        # Create old FileSet
        old_hash = hashlib.md5(b"old_a.pyold_b.py").hexdigest()
        old_fs = FileSet(snapshot_slug=snapshot_slug, files_hash=old_hash)
        session.add(old_fs)
        session.flush()
        for path in ["old_a.py", "old_b.py"]:
            session.add(FileSetMember(snapshot_slug=snapshot_slug, files_hash=old_hash, file_path=path))

        session.commit()

    # Session 2: Delete old FileSet and create new one IN THE SAME TRANSACTION
    with db.session() as session:
        # Delete old FileSet (CASCADE will delete its members)
        old_hash = hashlib.md5(b"old_a.pyold_b.py").hexdigest()
        session.query(FileSet).filter_by(snapshot_slug=snapshot_slug, files_hash=old_hash).delete()

        # Create new FileSet (like sync does after deletes)
        new_hash = hashlib.md5(b"new_a.pynew_b.py").hexdigest()
        new_fs = FileSet(snapshot_slug=snapshot_slug, files_hash=new_hash)
        session.add(new_fs)
        session.flush()  # ensure FK for members
        for path in ["new_a.py", "new_b.py"]:
            session.add(FileSetMember(snapshot_slug=snapshot_slug, files_hash=new_hash, file_path=path))
        # No flush after members

        session.commit()

    # Session 3: Check if new FileSet members persisted
    with db.session() as session:
        new_hash = hashlib.md5(b"new_a.pynew_b.py").hexdigest()
        stmt = select(FileSetMember).where(
            FileSetMember.snapshot_slug == snapshot_slug, FileSetMember.files_hash == new_hash
        )
        results = list(session.execute(stmt).scalars())

        # If there's an interaction between CASCADE delete and new inserts, members might not persist
        assert len(results) == 2, f"Expected 2 new members after delete+create, got {len(results)}"


if __name__ == "__main__":
    pytest_bazel.main()
