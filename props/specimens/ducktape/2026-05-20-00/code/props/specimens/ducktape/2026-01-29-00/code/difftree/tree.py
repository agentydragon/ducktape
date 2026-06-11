"""Build tree structure from file paths with aggregated statistics.

This module handles the DATA layer - pure representation of file tree structure
and diff statistics. No view concerns like path collapsing or styling.

TreeNode is immutable (frozen dataclass) - all tree transformations return new nodes.
View-level rendering logic (path collapsing, tree decoration styling) lives in diff_tree.py.
"""

from dataclasses import dataclass, field
from pathlib import Path

from difftree.config import SortMode
from difftree.parser import FileChange


@dataclass(frozen=True)
class TreeNode:
    """Immutable node in the file tree representing a file or directory.

    This is a pure data structure - no view concerns like path collapsing.
    Stats (additions, deletions) are aggregated from children.
    """

    name: str  # Component name (e.g., "foo.py" not "dir/foo.py")
    is_file: bool
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    children: dict[str, "TreeNode"] = field(default_factory=dict)
    path: str = ""  # Full path from root (e.g., "dir/foo.py")

    @property
    def total_changes(self) -> int:
        """Total number of line changes (additions + deletions)."""
        return self.additions + self.deletions


def build_tree(changes: list[FileChange]) -> TreeNode:
    """Build an immutable tree structure from file changes.

    Uses a mutable builder structure internally, then creates frozen TreeNodes.
    """

    @dataclass
    class _MutableNode:
        """Mutable builder for TreeNode."""

        name: str
        is_file: bool
        additions: int = 0
        deletions: int = 0
        is_binary: bool = False
        children: dict[str, "_MutableNode"] = field(default_factory=dict)
        path: str = ""

    # Build mutable tree
    root_name = Path.cwd().name or "."
    root = _MutableNode(name=root_name, is_file=False, path=".")

    for change in changes:
        parts = Path(change.path).parts
        current = root

        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            path_so_far = str(Path(*parts[: i + 1]))

            if part not in current.children:
                node = _MutableNode(
                    name=part,
                    is_file=is_last,
                    additions=change.additions if is_last else 0,
                    deletions=change.deletions if is_last else 0,
                    is_binary=change.is_binary if is_last else False,
                    path=path_so_far,
                )
                current.children[part] = node
                current = node
            else:
                current = current.children[part]
                if is_last:
                    current.additions = change.additions
                    current.deletions = change.deletions
                    current.is_binary = change.is_binary

    # Propagate stats bottom-up
    def propagate_stats(node: _MutableNode) -> tuple[int, int]:
        if node.is_file:
            return (node.additions, node.deletions)

        total_adds = 0
        total_dels = 0
        for child in node.children.values():
            adds, dels = propagate_stats(child)
            total_adds += adds
            total_dels += dels

        node.additions = total_adds
        node.deletions = total_dels
        return (total_adds, total_dels)

    propagate_stats(root)

    # Convert to immutable TreeNode
    def to_frozen(node: _MutableNode) -> TreeNode:
        frozen_children = {name: to_frozen(child) for name, child in node.children.items()}
        return TreeNode(
            name=node.name,
            is_file=node.is_file,
            additions=node.additions,
            deletions=node.deletions,
            is_binary=node.is_binary,
            children=frozen_children,
            path=node.path,
        )

    return to_frozen(root)


def sort_tree(node: TreeNode, sort_by: SortMode = SortMode.SIZE, reverse: bool = True) -> TreeNode:
    """Return a new tree with nodes sorted according to the specified mode.

    Since TreeNode is immutable, this creates a new tree with sorted children.
    """
    if not node.children:
        return node

    # Recursively sort children
    sorted_child_nodes = {name: sort_tree(child, sort_by, reverse) for name, child in node.children.items()}

    # Sort the children dict
    if sort_by == SortMode.SIZE:
        sorted_items = sorted(sorted_child_nodes.items(), key=lambda x: x[1].total_changes, reverse=reverse)
    else:
        sorted_items = sorted(sorted_child_nodes.items(), key=lambda x: x[0], reverse=False)

    sorted_children = dict(sorted_items)

    # Return new node with sorted children
    return TreeNode(
        name=node.name,
        is_file=node.is_file,
        additions=node.additions,
        deletions=node.deletions,
        is_binary=node.is_binary,
        children=sorted_children,
        path=node.path,
    )
