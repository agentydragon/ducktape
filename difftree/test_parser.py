"""Tests for git diff parser."""

from pathlib import Path

import pytest_bazel

from difftree.conftest import PNG_HEADER, create_file, git_add_commit
from difftree.parser import FileChange, parse_git_diff, parse_unified_diff


def test_parse_git_diff_with_changes(temp_git_repo: Path, run_git, monkeypatch):
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    create_file(temp_git_repo, "file2.py", "line1\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\nline4\n")
    create_file(temp_git_repo, "file2.py", "")

    monkeypatch.chdir(temp_git_repo)
    changes = parse_git_diff(None)
    # Plain `git diff` covers tracked modifications only.
    assert {c.path: (c.additions, c.deletions) for c in changes} == {"file1.py": (2, 0), "file2.py": (0, 1)}


def test_file_change_with_binary(temp_git_repo: Path, run_git):
    binary_file = temp_git_repo / "image.png"
    binary_file.write_bytes(PNG_HEADER + b"\x00" * 100)
    git_add_commit(run_git)

    binary_file.write_bytes(PNG_HEADER + b"\xff" * 100)

    result = run_git("diff")
    changes = parse_unified_diff(result.stdout)

    binary_change = next(c for c in changes if c.path == "image.png")
    assert binary_change == FileChange(path="image.png", additions=0, deletions=0, is_binary=True)


if __name__ == "__main__":
    pytest_bazel.main()
