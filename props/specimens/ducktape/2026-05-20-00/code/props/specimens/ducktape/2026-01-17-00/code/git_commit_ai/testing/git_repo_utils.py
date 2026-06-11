"""LLM test-local pygit2 helpers to avoid duplication in tests.

Kept separate from WT test data/helpers by design.
"""

from __future__ import annotations

from pathlib import Path

import pygit2


class RepoHelper:
    """Test helper with write/stage/commit methods.

    Use git_repo fixture for direct pygit2.Repository access.
    """

    def __init__(self, repo: pygit2.Repository):
        self.repo = repo

    def write(self, relpath: str, content: str) -> Path:
        p = Path(self.repo.workdir) / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def stage(self, relpath: str) -> None:
        self.repo.index.add(relpath)
        self.repo.index.write()

    def commit(self, message: str) -> None:
        sig = pygit2.Signature("Test User", "test@example.com")
        tree_oid = self.repo.index.write_tree()
        try:
            parent = self.repo.head.peel(pygit2.Commit)
            parents = [parent.id]
        except (KeyError, pygit2.GitError):
            parents = []
        self.repo.create_commit("HEAD", sig, sig, message, tree_oid, parents)


def _init_repo(tmpdir: str, name: str = "Test User", email: str = "test@example.com") -> pygit2.Repository:
    repo = pygit2.init_repository(tmpdir, initial_head="main")
    cfg = repo.config
    cfg["user.name"] = name
    cfg["user.email"] = email
    return repo
