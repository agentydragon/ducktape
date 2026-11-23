"""Shared test fixtures for precommit checks."""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(tmp_path / "repo"))


def commit(repo: pygit2.Repository) -> None:
    """Create a commit from the current index."""
    sig = pygit2.Signature("Test", "test@test.com")
    tree = repo.index.write_tree()
    parents = [repo.head.target] if not repo.head_is_unborn else []
    repo.create_commit("HEAD", sig, sig, "commit", tree, parents)


def stage(repo: pygit2.Repository, relpath: str, content: str = "# test") -> None:
    path = Path(repo.workdir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    repo.index.add(relpath)
    repo.index.write()
