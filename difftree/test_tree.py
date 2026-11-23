"""Tests for tree structure building."""

import pytest_bazel

from difftree.parser import FileChange
from difftree.tree import TreeNode, build_tree


def test_build_tree_single_file():
    changes = [FileChange(path="test.py", additions=10, deletions=5)]
    root = build_tree(changes)

    assert not root.is_file
    assert root.additions == 10
    assert root.deletions == 5
    assert root.children == (TreeNode(name="test.py", is_file=True, additions=10, deletions=5),)


if __name__ == "__main__":
    pytest_bazel.main()
