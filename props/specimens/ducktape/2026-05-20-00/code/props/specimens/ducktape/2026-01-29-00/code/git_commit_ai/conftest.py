from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from git_commit_ai.testing import git_repo_utils
from git_commit_ai.testing.git_repo_utils import RepoHelper

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from mcp_infra.testing.fixtures import *  # noqa: F403


@pytest.fixture
def author_name() -> str:
    return "Test User"


@pytest.fixture
def author_email() -> str:
    return "test@example.com"


@pytest.fixture
def git_repo(author_name: str, author_email: str, tmp_path: Path) -> pygit2.Repository:
    """Raw pygit2.Repository for tests needing direct access."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return git_repo_utils._init_repo(str(repo_dir), name=author_name, email=author_email)


@pytest.fixture
def temp_repo(git_repo: pygit2.Repository) -> RepoHelper:
    """Test helper wrapping git_repo with write/stage/commit methods."""
    return RepoHelper(git_repo)


