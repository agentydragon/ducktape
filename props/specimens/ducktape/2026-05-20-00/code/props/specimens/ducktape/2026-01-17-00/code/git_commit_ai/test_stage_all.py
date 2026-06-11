"""Tests for -a/--all flag staging behavior."""

from __future__ import annotations

from pathlib import Path

import pygit2

from .cli import stage_tracked_changes
from .testing.git_repo_utils import RepoHelper


def test_stage_all_includes_modified_files(temp_repo: RepoHelper) -> None:
    """Modified tracked files should be staged."""
    temp_repo.write("file.txt", "initial")
    temp_repo.stage("file.txt")
    temp_repo.commit("initial")

    temp_repo.write("file.txt", "modified")

    stage_tracked_changes(temp_repo.repo)

    status = temp_repo.repo.status()
    assert "file.txt" in status
    assert status["file.txt"] & pygit2.GIT_STATUS_INDEX_MODIFIED


def test_stage_all_includes_deleted_files(temp_repo: RepoHelper) -> None:
    """Deleted tracked files should be staged for removal."""
    temp_repo.write("file.txt", "content")
    temp_repo.stage("file.txt")
    temp_repo.commit("initial")

    (Path(temp_repo.repo.workdir) / "file.txt").unlink()

    stage_tracked_changes(temp_repo.repo)

    status = temp_repo.repo.status()
    assert "file.txt" in status
    assert status["file.txt"] & pygit2.GIT_STATUS_INDEX_DELETED


def test_stage_all_excludes_untracked_files(temp_repo: RepoHelper) -> None:
    """New untracked files should NOT be staged (matches git commit -a)."""
    temp_repo.write("tracked.txt", "content")
    temp_repo.stage("tracked.txt")
    temp_repo.commit("initial")

    temp_repo.write("untracked.txt", "new file")

    stage_tracked_changes(temp_repo.repo)

    status = temp_repo.repo.status()
    assert "untracked.txt" in status
    # Should still be WT_NEW (untracked), not INDEX_NEW (staged)
    assert status["untracked.txt"] & pygit2.GIT_STATUS_WT_NEW
    assert not (status["untracked.txt"] & pygit2.GIT_STATUS_INDEX_NEW)


def test_stage_all_handles_mixed_changes(temp_repo: RepoHelper) -> None:
    """Mix of modified, deleted, and untracked files."""
    temp_repo.write("modify.txt", "initial")
    temp_repo.write("delete.txt", "to delete")
    temp_repo.stage("modify.txt")
    temp_repo.stage("delete.txt")
    temp_repo.commit("initial")

    temp_repo.write("modify.txt", "changed")
    (Path(temp_repo.repo.workdir) / "delete.txt").unlink()
    temp_repo.write("untracked.txt", "new")

    stage_tracked_changes(temp_repo.repo)

    status = temp_repo.repo.status()

    # Modified file should be staged
    assert status["modify.txt"] & pygit2.GIT_STATUS_INDEX_MODIFIED

    # Deleted file should be staged for removal
    assert status["delete.txt"] & pygit2.GIT_STATUS_INDEX_DELETED

    # Untracked file should remain untracked
    assert status["untracked.txt"] & pygit2.GIT_STATUS_WT_NEW
    assert not (status["untracked.txt"] & pygit2.GIT_STATUS_INDEX_NEW)
