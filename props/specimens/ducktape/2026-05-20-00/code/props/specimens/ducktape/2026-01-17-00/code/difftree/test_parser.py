"""Tests for git diff parser."""

from pathlib import Path

from difftree.conftest import PNG_HEADER, create_file, git_add_commit
from difftree.parser import FileChange, parse_git_diff, parse_unified_diff


def test_file_change_dataclass():
    """Test FileChange dataclass properties."""
    change = FileChange(path="test.py", additions=10, deletions=5)

    assert change == FileChange(path="test.py", additions=10, deletions=5, is_binary=False)
    assert change.total_changes == 15


def test_parse_git_diff_with_changes(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    create_file(temp_git_repo, "file2.py", "line1\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\nline4\n")
    create_file(temp_git_repo, "file2.py", "")
    create_file(temp_git_repo, "file3.py", "new file\n")

    changes = parse_git_diff(None)
    assert isinstance(changes, list)


def test_parse_git_diff_empty(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\n")
    git_add_commit(run_git)

    result = run_git("diff")
    assert result.stdout.strip() == ""


def test_file_change_with_binary(temp_git_repo: Path, run_git):
    binary_file = temp_git_repo / "image.png"
    binary_file.write_bytes(PNG_HEADER + b"\x00" * 100)
    git_add_commit(run_git)

    binary_file.write_bytes(PNG_HEADER + b"\xff" * 100)

    result = run_git("diff")
    changes = parse_unified_diff(result.stdout)

    binary_change = next(c for c in changes if c.path == "image.png")
    assert binary_change == FileChange(path="image.png", additions=0, deletions=0, is_binary=True)
