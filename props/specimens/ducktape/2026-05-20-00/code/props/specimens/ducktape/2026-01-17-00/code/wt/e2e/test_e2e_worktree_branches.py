import os

import pygit2
import pytest

from wt.testing.git_helpers import add_worktree

pytestmark = pytest.mark.timeout(10)


def test_worktree_branch_names_are_actual(repo_factory, config_factory, wtcli, require_gitstatusd):
    repo_path = repo_factory.create_repo()
    cfg = config_factory(repo_path).minimal(upstream_branch="HEAD")

    # Create two worktrees against branches test/aaaaa and test/bbbbb
    repo = pygit2.Repository(repo_path)
    head_commit = repo[repo.head.target]
    assert isinstance(head_commit, pygit2.Commit)
    repo.create_branch("test/aaaaa", head_commit)
    repo.create_branch("test/bbbbb", head_commit)

    wt_a = repo_path / "worktrees" / "aaaaa"
    wt_b = repo_path / "worktrees" / "bbbbb"

    # Create worktrees directory
    (repo_path / "worktrees").mkdir(exist_ok=True)

    # Use pygit2 to add worktrees
    add_worktree(repo, wt_a, "test/aaaaa")
    add_worktree(repo, wt_b, "test/bbbbb")

    # Use GitManager via daemon handlers indirectly by calling CLI ls (list)
    env = os.environ.copy()
    env["WT_DIR"] = str(cfg.wt_dir)

    # Start daemon by calling status once
    wtcli(env).status()

    # Call list command and ensure worktrees are present (ls output shows names/paths)
    res = wtcli(env).sh("ls")
    assert res.returncode == 0
    out = res.stdout
    assert "aaaaa:" in out
    assert "bbbbb:" in out

    # Additionally, query actual worktree branch heads (branch can change over time)
    repo_a = pygit2.Repository(wt_a)
    repo_b = pygit2.Repository(wt_b)
    assert repo_a.head.shorthand == "test/aaaaa"
    assert repo_b.head.shorthand == "test/bbbbb"
