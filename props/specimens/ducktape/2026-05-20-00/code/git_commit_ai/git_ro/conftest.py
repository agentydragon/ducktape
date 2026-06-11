from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from git_commit_ai.git_ro.server import GitRoServer
from mcp_infra.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode."""
    config.option.asyncio_mode = "auto"


def _ensure_identity(repo: pygit2.Repository) -> None:
    cfg = repo.config
    cfg["user.name"] = "Test"
    cfg["user.email"] = "test@example.com"


def _commit_all(repo: pygit2.Repository, message: str) -> None:
    idx = repo.index
    idx.add_all()
    idx.write()
    tree = idx.write_tree()
    parents: list[pygit2.Oid | str] = []
    if not repo.head_is_unborn:
        # repo.head.target is Oid | str (hex hash string)
        parents = [repo.head.target]
    author = committer = pygit2.Signature("Test", "test@example.com")
    repo.create_commit("HEAD", author, committer, message, tree, parents)


@pytest.fixture
def repo_git_ro(tmp_path: Path) -> pygit2.Repository:
    """Single unified repo for git-ro tests.

    Commits:
      1) README
      2) add file1
      3) rename file1 -> file_renamed.txt + modify
    Also stages a large big.txt (uncommitted) for diff pagination tests.
    """
    repo_path = tmp_path / "repo_git_ro"
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(repo_path), bare=False, initial_head="main")
    _ensure_identity(repo)

    (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _commit_all(repo, "init")

    (repo_path / "file1.txt").write_text("hello\n", encoding="utf-8")
    _commit_all(repo, "add file1")

    (repo_path / "file1.txt").rename(repo_path / "file_renamed.txt")
    (repo_path / "file_renamed.txt").write_text("hello world\n", encoding="utf-8")
    _commit_all(repo, "rename + modify")

    big = repo_path / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(20000)) + "\n", encoding="utf-8")
    idx = repo.index
    idx.add(str(big.relative_to(repo_path)))
    idx.write()

    return repo


@pytest.fixture
async def typed_git_ro(repo_git_ro: pygit2.Repository, make_typed_mcp):
    """Async yield fixture providing a TypedClient for git-ro server.

    Usage:
        async def test_something(typed_git_ro):
            result = await typed_git_ro.diff(...)
    """
    server = GitRoServer(repo_git_ro)
    async with make_typed_mcp(server) as (client, _session):
        yield client


@pytest.fixture
def repo_with_conflict(tmp_path: Path) -> pygit2.Repository:
    """Repo with a merge conflict for testing :N:path syntax.

    Creates a conflict in 'conflict.txt' with:
      - Stage 1 (ancestor): "ancestor content"
      - Stage 2 (ours): "ours content"
      - Stage 3 (theirs): "theirs content"
    """
    repo_path = tmp_path / "repo_conflict"
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(repo_path), bare=False, initial_head="main")
    _ensure_identity(repo)

    # Create initial commit with base content
    (repo_path / "conflict.txt").write_text("ancestor content\n", encoding="utf-8")
    _commit_all(repo, "initial")
    base_commit = repo.head.peel(pygit2.Commit)

    # Create branch "theirs" with different content
    repo.branches.local.create("theirs", base_commit)
    repo.checkout(repo.branches["theirs"])
    (repo_path / "conflict.txt").write_text("theirs content\n", encoding="utf-8")
    _commit_all(repo, "theirs change")
    theirs_commit = repo.head.peel(pygit2.Commit)

    # Go back to main and make conflicting change
    repo.checkout(repo.branches["main"])
    (repo_path / "conflict.txt").write_text("ours content\n", encoding="utf-8")
    _commit_all(repo, "ours change")

    # Merge theirs into main to create conflict
    repo.merge(theirs_commit.id)

    return repo


@pytest.fixture
async def typed_git_ro_conflict(repo_with_conflict: pygit2.Repository, make_typed_mcp):
    """TypedClient for repo with merge conflict."""
    server = GitRoServer(repo_with_conflict)
    async with make_typed_mcp(server) as (client, _session):
        yield client


@pytest.fixture
def repo_with_new_file(tmp_path: Path) -> pygit2.Repository:
    """Repo with a newly added file (staged but not in any commit).

    Creates:
      - One commit with README.md
      - A new file 'src/newfile.py' staged but not committed

    This tests the case where HEAD:path fails but :path works.
    """
    repo_path = tmp_path / "repo_new_file"
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(repo_path), bare=False, initial_head="main")
    _ensure_identity(repo)

    # Create initial commit
    (repo_path / "README.md").write_text("hello\n", encoding="utf-8")
    _commit_all(repo, "initial")

    # Add new file in a subdirectory (staged only, not committed)
    src_dir = repo_path / "src"
    src_dir.mkdir()
    (src_dir / "newfile.py").write_text("# new file content\nprint('hello')\n", encoding="utf-8")
    idx = repo.index
    idx.add("src/newfile.py")
    idx.write()

    return repo


@pytest.fixture
async def typed_git_ro_new_file(repo_with_new_file: pygit2.Repository, make_typed_mcp):
    """TypedClient for repo with newly added file."""
    server = GitRoServer(repo_with_new_file)
    async with make_typed_mcp(server) as (client, _session):
        yield client
