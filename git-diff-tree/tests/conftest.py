"""Shared test fixtures for git-diff-tree tests."""

import subprocess
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from git_diff_tree.parser import FileChange


@pytest.fixture
def sample_changes() -> list[FileChange]:
    """Sample file changes for testing."""
    return [
        FileChange(path="src/main.py", additions=10, deletions=2),
        FileChange(path="src/utils.py", additions=5, deletions=0),
        FileChange(path="src/models/user.py", additions=20, deletions=5),
        FileChange(path="src/models/post.py", additions=15, deletions=3),
        FileChange(path="tests/test_main.py", additions=8, deletions=1),
        FileChange(path="README.md", additions=3, deletions=0),
    ]


@pytest.fixture
def temp_git_repo() -> Generator[Path, None, None]:
    """
    Create a temporary git repository for E2E testing.

    Yields:
        Path to the temporary git repository.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize git repo
        subprocess.run(
            ["git", "init"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        # Disable commit signing for tests
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

        yield repo_path


def create_file(repo_path: Path, file_path: str, content: str) -> None:
    """
    Create a file in the repository.

    Args:
        repo_path: Path to the git repository.
        file_path: Relative path to the file.
        content: File content.
    """
    full_path = repo_path / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)


def git_add_commit(repo_path: Path, message: str) -> None:
    """
    Add all files and commit.

    Args:
        repo_path: Path to the git repository.
        message: Commit message.
    """
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
