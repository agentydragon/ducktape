"""Shared test fixtures for difftree tests."""

import subprocess
from io import StringIO
from pathlib import Path
from typing import Literal

import pytest
from rich.console import Console

from difftree.config import RenderConfig, SortMode
from difftree.diff_tree import DiffTree
from difftree.parser import FileChange
from difftree.progress_bar import DEFAULT_LEFT_BLOCKS, DEFAULT_RIGHT_BLOCKS
from difftree.tree import build_tree, sort_tree

# Test constants
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

# Block character constants for assertions
# Import from progress_bar to avoid hardcoding in tests
LEFT_BLOCK_CHARS = (DEFAULT_LEFT_BLOCKS.full, *DEFAULT_LEFT_BLOCKS.partials)
RIGHT_BLOCK_CHARS = (DEFAULT_RIGHT_BLOCKS.full, *DEFAULT_RIGHT_BLOCKS.partials)


ColorSystem = Literal["auto", "standard", "256", "truecolor", "windows"] | None


def render_to_string(
    renderable,
    width: int = 80,
    force_terminal: bool = True,
    legacy_windows: bool = False,
    color_system: ColorSystem = "standard",
) -> str:
    """Render a Rich renderable to a string at a specific width.

    Args:
        renderable: The Rich renderable object to render
        width: Console width
        force_terminal: Whether to force terminal mode
        legacy_windows: Legacy Windows mode setting
        color_system: Color system to use (e.g., "standard", None)

    Returns:
        The rendered string output
    """
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=force_terminal,
        width=width,
        legacy_windows=legacy_windows,
        color_system=color_system,
    )
    console.print(renderable)
    return output.getvalue()


def make_diff_tree(
    changes: list[FileChange], config: RenderConfig | None = None, sort_by: SortMode = SortMode.ALPHA
) -> DiffTree:
    """Build a DiffTree from file changes."""
    root = build_tree(changes)
    root = sort_tree(root, sort_by=sort_by)
    return DiffTree(root, config=config)


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
def temp_git_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a temporary git repository for E2E testing."""
    repo_path = tmp_path_factory.mktemp("git_repo")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
    # Disable commit signing for tests
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo_path, check=True, capture_output=True)

    return repo_path


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


def git_add_commit(run_git) -> None:
    """
    Add all files and commit with a default message.

    Args:
        run_git: Fixture for running git commands.
    """
    run_git("add", ".")
    run_git("commit", "-m", "test commit")


@pytest.fixture
def run_git(temp_git_repo: Path):
    """
    Fixture factory for running git commands in the test repository.

    Returns:
        Callable that runs git commands with the given arguments.

    Example:
        def test_foo(run_git):
            result = run_git("diff", "--numstat")
            assert "file.py" in result.stdout
    """

    def _run_git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=temp_git_repo, capture_output=True, text=True, check=True)

    return _run_git
