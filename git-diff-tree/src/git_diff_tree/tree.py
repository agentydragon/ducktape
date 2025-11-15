"""Build tree structure from file paths with aggregated statistics."""

from dataclasses import dataclass, field
from pathlib import Path

from .parser import FileChange


@dataclass
class TreeNode:
    """A node in the file tree (either a file or directory)."""

    name: str
    is_file: bool
    additions: int = 0
    deletions: int = 0
    children: dict[str, "TreeNode"] = field(default_factory=dict)
    path: str = ""

    @property
    def total_changes(self) -> int:
        """Total number of line changes (additions + deletions)."""
        return self.additions + self.deletions

    def add_child(self, name: str, node: "TreeNode") -> None:
        """Add a child node and update parent statistics."""
        self.children[name] = node
        # Aggregate stats from children
        if not self.is_file:
            self.additions += node.additions
            self.deletions += node.deletions


def build_tree(changes: list[FileChange]) -> TreeNode:
    """
    Build a tree structure from a list of file changes.

    Args:
        changes: List of FileChange objects.

    Returns:
        Root TreeNode representing the tree structure.
    """
    root = TreeNode(name=".", is_file=False, path=".")

    for change in changes:
        parts = Path(change.path).parts
        current = root

        # Navigate/create the tree structure
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            path_so_far = str(Path(*parts[: i + 1]))

            if part not in current.children:
                node = TreeNode(
                    name=part,
                    is_file=is_last,
                    additions=change.additions if is_last else 0,
                    deletions=change.deletions if is_last else 0,
                    path=path_so_far,
                )
                current.add_child(part, node)
                current = node
            else:
                current = current.children[part]
                # If this is the file itself, update its stats
                if is_last:
                    current.additions = change.additions
                    current.deletions = change.deletions

        # Propagate stats upward
        _propagate_stats_upward(root)

    return root


def _propagate_stats_upward(node: TreeNode) -> tuple[int, int]:
    """
    Recursively propagate statistics from leaves to root.

    Returns:
        Tuple of (additions, deletions) for this node.
    """
    if node.is_file:
        return (node.additions, node.deletions)

    total_additions = 0
    total_deletions = 0

    for child in node.children.values():
        child_additions, child_deletions = _propagate_stats_upward(child)
        total_additions += child_additions
        total_deletions += child_deletions

    node.additions = total_additions
    node.deletions = total_deletions

    return (total_additions, total_deletions)


def sort_tree(
    node: TreeNode,
    sort_by: str = "size",
    reverse: bool = True,
) -> None:
    """
    Sort tree nodes in place.

    Args:
        node: TreeNode to sort (modifies in place).
        sort_by: "size" for total changes, "alpha" for alphabetical.
        reverse: Sort in descending order if True.
    """
    if not node.children:
        return

    # Sort children first (recursively)
    for child in node.children.values():
        sort_tree(child, sort_by, reverse)

    # Sort current level
    if sort_by == "size":
        sorted_children = sorted(
            node.children.items(),
            key=lambda x: x[1].total_changes,
            reverse=reverse,
        )
    else:  # alpha
        sorted_children = sorted(
            node.children.items(),
            key=lambda x: x[0],
            reverse=False,  # Always ascending for alpha
        )

    # Rebuild children dict in sorted order
    node.children = dict(sorted_children)
