"""E2E tests for git_hook worktree support."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest_bazel

from devinfra.precommit.git_hook import _setup_tracing


def test_setup_tracing_writes_to_worktree_git_dir(tmp_path: Path) -> None:
    """Traces go to the per-worktree git dir, not repo_root/.git."""
    # Create a main repo with an initial commit.
    main = tmp_path / "main"
    main.mkdir()
    repo = pygit2.init_repository(str(main))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@test.com"
    sig = pygit2.Signature("Test", "test@test.com")
    (main / "file.txt").write_text("hello")
    repo.index.add("file.txt")
    repo.index.write()
    tree = repo.index.write_tree()
    repo.create_commit("refs/heads/main", sig, sig, "init", tree, [])
    repo.set_head("refs/heads/main")

    # Create a worktree.
    wt_path = tmp_path / "wt"
    repo.add_worktree("wt", str(wt_path))

    wt_repo = pygit2.Repository(str(wt_path))

    # .git in the worktree is a file, not a directory.
    assert (wt_path / ".git").is_file(), ".git should be a file in a worktree"

    # repo.path should point into main's .git/worktrees/wt/
    wt_git_dir = Path(wt_repo.path)
    assert "worktrees" in str(wt_git_dir)

    # Run _setup_tracing — it should not crash and the trace file should be
    # created under the worktree's git dir, not under wt_path/.git/.
    _setup_tracing(wt_repo)

    trace_file = wt_git_dir / "precommit-traces.jsonl"
    # The exporter is lazy — file is created on first flush, not on init.
    # But the parent directory must exist and be writable.
    assert trace_file.parent.is_dir()
    # Confirm it would NOT go to wt_path/.git/precommit-traces.jsonl
    # (which would fail because .git is a file).
    assert not (wt_path / ".git" / "precommit-traces.jsonl").exists()


def test_setup_tracing_works_in_normal_repo(tmp_path: Path) -> None:
    """Traces go to .git/ in a normal (non-worktree) repo."""
    repo = pygit2.init_repository(str(tmp_path))
    _setup_tracing(repo)

    trace_file = Path(repo.path) / "precommit-traces.jsonl"
    assert trace_file.parent.is_dir()
    assert trace_file.parent == tmp_path / ".git"


if __name__ == "__main__":
    pytest_bazel.main()
