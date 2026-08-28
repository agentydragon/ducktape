from pathlib import Path

import pytest_bazel

from difftree.config import SortMode
from difftree.conftest import create_file, git_add_commit
from difftree.parser import parse_unified_diff
from difftree.tree import build_tree, sort_tree


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

    assert len(changes) > 0
    assert "src" in root.children

    root = sort_tree(root, sort_by=SortMode.SIZE)
    assert root is not None


if __name__ == "__main__":
    pytest_bazel.main()
