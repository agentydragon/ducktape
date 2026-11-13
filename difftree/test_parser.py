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


def test_parse_diff_preserves_paths_starting_with_a_or_b(temp_git_repo: Path, run_git):
    nested_files = [temp_git_repo / prefix / "file.py" for prefix in ("a", "b")]
    for nested_file in nested_files:
        nested_file.parent.mkdir()
        nested_file.write_text("before\n")
    git_add_commit(run_git)

    for nested_file in nested_files:
        nested_file.write_text("after\n")
    changes = parse_unified_diff(run_git("diff").stdout)

    assert {change.path: change.total_changes for change in changes} == {"a/file.py": 2, "b/file.py": 2}


def test_parse_deleted_paths_starting_with_a_or_b(temp_git_repo: Path, run_git):
    nested_files = [temp_git_repo / prefix / "file.py" for prefix in ("a", "b")]
    for nested_file in nested_files:
        nested_file.parent.mkdir()
        nested_file.write_text("before\n")
    git_add_commit(run_git)

    for nested_file in nested_files:
        nested_file.unlink()
    changes = parse_unified_diff(run_git("diff").stdout)

    assert {change.path: (change.additions, change.deletions) for change in changes} == {
        "a/file.py": (0, 1),
        "b/file.py": (0, 1),
    }


def test_unprefixed_path_without_git_prefix_is_unchanged(temp_git_repo: Path, run_git):
    nested_file = temp_git_repo / "src" / "file.py"
    nested_file.parent.mkdir()
    nested_file.write_text("before\n")
    git_add_commit(run_git)

    nested_file.write_text("after\n")
    changes = parse_unified_diff(run_git("diff", "--no-prefix").stdout)

    assert changes == [FileChange(path="src/file.py", additions=1, deletions=1)]


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
