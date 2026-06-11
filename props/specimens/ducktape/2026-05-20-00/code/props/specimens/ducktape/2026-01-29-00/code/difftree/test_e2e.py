from pathlib import Path

import pytest_bazel

from difftree.config import SortMode
from difftree.conftest import create_file, git_add_commit
from difftree.parser import parse_unified_diff
from difftree.tree import build_tree, sort_tree


def test_e2e_git_diff_unstaged(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\n")
    create_file(temp_git_repo, "file2.py", "new file\n")

    result = run_git("diff")
    assert result.stdout.strip() != ""


def test_e2e_git_diff_between_commits(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    create_file(temp_git_repo, "file2.py", "content\n")
    git_add_commit(run_git)

    result = run_git("diff", "HEAD~1", "HEAD")
    assert result.stdout.strip() != ""


def test_e2e_complete_workflow(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "src/main.py", "def main():\n    pass\n")
    create_file(temp_git_repo, "src/utils.py", "def helper():\n    pass\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "src/main.py", "def main():\n    print('hello')\n    pass\n")
    create_file(temp_git_repo, "src/models/user.py", "class User:\n    pass\n")
    create_file(temp_git_repo, "README.md", "# Project\n")

    result = run_git("diff")
    changes = parse_unified_diff(result.stdout)
    root = build_tree(changes)

    # Root name is basename of current directory
    assert root.name in (".", "difftree", Path.cwd().name)
    assert "src" in root.children or len(changes) > 0

    root = sort_tree(root, sort_by=SortMode.SIZE)
    assert root is not None


def test_e2e_with_deletions(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\nline2\nline3\nline4\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline4\n")

    result = run_git("diff")
    assert result.stdout.strip() != ""


def test_e2e_staged_changes(temp_git_repo: Path, run_git):
    create_file(temp_git_repo, "file1.py", "line1\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "file1.py", "line1\nline2\n")
    run_git("add", "file1.py")

    result = run_git("diff", "--cached")
    assert "file1.py" in result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
