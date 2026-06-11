"""Shared test fixtures for precommit checks."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    repo = pygit2.init_repository(str(tmp_path))
    repo.config["user.name"] = "Test"
    repo.config["user.email"] = "test@test.com"
    return repo


def commit(repo: pygit2.Repository) -> None:
    """Create a commit from the current index."""
    sig = pygit2.Signature("Test", "test@test.com")
    tree = repo.index.write_tree()
    parents = [repo.head.target] if not repo.head_is_unborn else []
    repo.create_commit("HEAD", sig, sig, "commit", tree, parents)


def staged_deltas(repo: pygit2.Repository) -> list[pygit2.DiffDelta]:
    """Compute staged deltas the same way the precommit entrypoint does."""
    base = repo[repo.TreeBuilder().write()].peel(pygit2.Tree) if repo.head_is_unborn else repo.head.peel(pygit2.Tree)
    repo.index.read()
    return list(repo.index.diff_to_tree(base).deltas)


def head_tree(repo: pygit2.Repository) -> pygit2.Tree | None:
    return None if repo.head_is_unborn else repo.head.peel(pygit2.Tree)
