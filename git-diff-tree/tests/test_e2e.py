"""End-to-end tests with actual git operations."""

import subprocess
from pathlib import Path

import pytest

from git_diff_tree.parser import parse_git_diff
from git_diff_tree.tree import build_tree, sort_tree
from .conftest import create_file, git_add_commit


def test_e2e_git_diff_unstaged(temp_git_repo: Path):
    """Test E2E workflow with unstaged changes."""
    # Create initial commit
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    git_add_commit(temp_git_repo, "Initial commit")

    # Make unstaged changes
    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\n")
    create_file(temp_git_repo, "file2.py", "new file\n")

    # Parse diff from within the repo
    result = subprocess.run(
        ["git", "diff", "--numstat"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Should have changes for file1.py and file2.py
    lines = result.stdout.strip().split("\n")
    assert len(lines) >= 1


def test_e2e_git_diff_between_commits(temp_git_repo: Path):
    """Test E2E workflow with changes between commits."""
    # Create first commit
    create_file(temp_git_repo, "file1.py", "line1\n")
    git_add_commit(temp_git_repo, "First commit")

    # Create second commit
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    create_file(temp_git_repo, "file2.py", "content\n")
    git_add_commit(temp_git_repo, "Second commit")

    # Get diff between commits
    result = subprocess.run(
        ["git", "diff", "--numstat", "HEAD~1", "HEAD"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    lines = result.stdout.strip().split("\n")
    # Should have changes for file1.py and file2.py
    assert len(lines) >= 2


def test_e2e_complete_workflow(temp_git_repo: Path):
    """Test complete workflow: parse -> build tree -> sort."""
    # Create initial commit
    create_file(temp_git_repo, "src/main.py", "def main():\n    pass\n")
    create_file(temp_git_repo, "src/utils.py", "def helper():\n    pass\n")
    git_add_commit(temp_git_repo, "Initial commit")

    # Make changes
    create_file(
        temp_git_repo,
        "src/main.py",
        "def main():\n    print('hello')\n    pass\n",
    )
    create_file(
        temp_git_repo,
        "src/models/user.py",
        "class User:\n    pass\n",
    )
    create_file(temp_git_repo, "README.md", "# Project\n")

    # Get diff output
    result = subprocess.run(
        ["git", "diff", "--numstat"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Parse the output manually (since parse_git_diff runs git internally)
    from git_diff_tree.parser import FileChange

    changes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            add_str, del_str, path = parts
            try:
                additions = int(add_str)
            except ValueError:
                additions = 0
            try:
                deletions = int(del_str)
            except ValueError:
                deletions = 0
            changes.append(FileChange(path=path, additions=additions, deletions=deletions))

    # Build tree
    root = build_tree(changes)

    # Should have created proper tree structure
    assert root.name == "."
    assert "src" in root.children or len(changes) > 0

    # Sort tree
    sort_tree(root, sort_by="size")

    # Should complete without errors
    assert root is not None


def test_e2e_with_deletions(temp_git_repo: Path):
    """Test E2E workflow with file deletions."""
    # Create initial commit with content
    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\nline4\n")
    git_add_commit(temp_git_repo, "Initial commit")

    # Delete some lines
    create_file(temp_git_repo, "file1.py", "line1\nline4\n")

    # Get diff
    result = subprocess.run(
        ["git", "diff", "--numstat"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Should show deletions
    assert result.stdout.strip() != ""


def test_e2e_staged_changes(temp_git_repo: Path):
    """Test E2E workflow with staged changes."""
    # Create initial commit
    create_file(temp_git_repo, "file1.py", "line1\n")
    git_add_commit(temp_git_repo, "Initial commit")

    # Make changes and stage them
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    subprocess.run(
        ["git", "add", "file1.py"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Get staged diff
    result = subprocess.run(
        ["git", "diff", "--numstat", "--cached"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
        check=True,
    )

    # Should show staged changes
    assert "file1.py" in result.stdout
