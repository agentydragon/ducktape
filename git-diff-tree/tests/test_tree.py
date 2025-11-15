"""Tests for tree structure building."""

import pytest

from git_diff_tree.parser import FileChange
from git_diff_tree.tree import build_tree, sort_tree


def test_build_tree_single_file():
    """Test building tree with a single file."""
    changes = [FileChange(path="test.py", additions=10, deletions=5)]
    root = build_tree(changes)

    assert root.name == "."
    assert not root.is_file
    assert root.additions == 10
    assert root.deletions == 5
    assert "test.py" in root.children

    test_file = root.children["test.py"]
    assert test_file.name == "test.py"
    assert test_file.is_file
    assert test_file.additions == 10
    assert test_file.deletions == 5


def test_build_tree_nested_files(sample_changes: list[FileChange]):
    """Test building tree with nested directory structure."""
    root = build_tree(sample_changes)

    # Check root aggregates all stats
    total_additions = sum(c.additions for c in sample_changes)
    total_deletions = sum(c.deletions for c in sample_changes)

    assert root.additions == total_additions
    assert root.deletions == total_deletions

    # Check directory structure
    assert "src" in root.children
    assert "tests" in root.children
    assert "README.md" in root.children

    src_dir = root.children["src"]
    assert not src_dir.is_file
    assert "main.py" in src_dir.children
    assert "utils.py" in src_dir.children
    assert "models" in src_dir.children

    models_dir = src_dir.children["models"]
    assert not models_dir.is_file
    assert "user.py" in models_dir.children
    assert "post.py" in models_dir.children


def test_tree_statistics_aggregation(sample_changes: list[FileChange]):
    """Test that directory statistics are correctly aggregated."""
    root = build_tree(sample_changes)

    # src/models should have stats from user.py and post.py
    models_dir = root.children["src"].children["models"]
    assert models_dir.additions == 20 + 15  # user.py + post.py
    assert models_dir.deletions == 5 + 3

    # src should have stats from all files under it
    src_dir = root.children["src"]
    expected_additions = 10 + 5 + 20 + 15  # main.py + utils.py + models/*
    expected_deletions = 2 + 0 + 5 + 3
    assert src_dir.additions == expected_additions
    assert src_dir.deletions == expected_deletions


def test_sort_tree_by_size(sample_changes: list[FileChange]):
    """Test sorting tree by total changes (descending)."""
    root = build_tree(sample_changes)
    sort_tree(root, sort_by="size", reverse=True)

    # Get children in order
    children_names = list(root.children.keys())

    # src should be first (most changes: 10+2+5+20+5+15+3 = 60)
    # tests should be second (8+1 = 9)
    # README.md should be last (3+0 = 3)
    assert children_names[0] == "src"
    assert children_names[-1] == "README.md"


def test_sort_tree_alphabetically(sample_changes: list[FileChange]):
    """Test sorting tree alphabetically."""
    root = build_tree(sample_changes)
    sort_tree(root, sort_by="alpha")

    # Get children in order
    children_names = list(root.children.keys())

    # Should be in alphabetical order
    assert children_names == sorted(children_names)


def test_tree_node_total_changes():
    """Test TreeNode.total_changes property."""
    from git_diff_tree.tree import TreeNode

    node = TreeNode(name="test", is_file=True, additions=10, deletions=5)
    assert node.total_changes == 15

    node2 = TreeNode(name="test2", is_file=True, additions=0, deletions=0)
    assert node2.total_changes == 0
