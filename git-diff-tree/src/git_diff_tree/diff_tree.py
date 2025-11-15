"""Render tree structure with rich formatting and progress bars."""

from typing import Optional

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from rich.tree import Tree

from .config import Column, RenderConfig
from .progress_bar import DualProgressBar
from .tree import TreeNode


class DiffTree:
    """A renderable diff tree with progress bars and statistics.

    Follows Rich's Renderable protocol - use with console.print(DiffTree(...))
    """

    def __init__(
        self,
        root: TreeNode,
        config: Optional[RenderConfig] = None,
    ):
        """
        Initialize the diff tree.

        Args:
            root: Root TreeNode to render.
            config: RenderConfig object (uses default if None).
        """
        self.root = root
        self.config = config or RenderConfig.default()

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Rich console protocol for rendering.

        Args:
            console: The console instance.
            options: Console rendering options.

        Yields:
            Rich Tree to render.
        """
        # Find max additions and deletions separately for consistent bar breakpoints
        max_additions, max_deletions = self._find_max_additions_deletions(self.root)
        max_changes = (
            max_additions + max_deletions if (max_additions + max_deletions) > 0 else 1
        )

        # Build Rich Tree and yield
        tree = self._build_rich_tree(
            self.root,
            max_changes,
            max_additions,
            max_deletions,
            self.config.max_depth,
            depth=0,
        )
        yield tree

    def _find_max_additions_deletions(self, node: TreeNode) -> tuple[int, int]:
        """
        Find the maximum additions and deletions separately across all nodes.

        This ensures bars align at a consistent breakpoint across all files.

        Args:
            node: TreeNode to search.

        Returns:
            Tuple of (max_additions, max_deletions).
        """
        max_additions = node.additions
        max_deletions = node.deletions

        for child in node.children.values():
            child_max_add, child_max_del = self._find_max_additions_deletions(child)
            max_additions = max(max_additions, child_max_add)
            max_deletions = max(max_deletions, child_max_del)

        return max_additions, max_deletions

    def _build_rich_tree(
        self,
        node: TreeNode,
        max_changes: int,
        max_additions: int,
        max_deletions: int,
        max_depth: Optional[int],
        depth: int = 0,
    ) -> Tree:
        """
        Build a Rich Tree from a TreeNode.

        Args:
            node: TreeNode to convert.
            max_changes: Maximum total changes (for percentage calculation).
            max_additions: Maximum additions across all nodes (for bar scaling).
            max_deletions: Maximum deletions across all nodes (for bar scaling).
            max_depth: Maximum depth to render (None for unlimited).
            depth: Current depth in tree.

        Returns:
            Rich Tree object.
        """
        # Create label for this node
        label = self._make_node_label(node, max_changes, max_additions, max_deletions)

        # Create Rich Tree with the label
        tree = Tree(label)

        # Add children if within depth limit
        if (
            (max_depth is None or depth < max_depth)
            and not node.is_file
            and node.children
        ):
            for child in node.children.values():
                child_tree = self._build_rich_tree(
                    child,
                    max_changes,
                    max_additions,
                    max_deletions,
                    max_depth,
                    depth + 1,
                )
                tree.add(child_tree)

        return tree

    def _make_node_label(
        self, node: TreeNode, max_changes: int, max_additions: int, max_deletions: int
    ) -> Text:
        """
        Create the label for a tree node with stats and progress bars.

        Bars are scaled independently so the breakpoint between additions (green)
        and deletions (red) is at a consistent position across all files.

        Args:
            node: TreeNode to create label for.
            max_changes: Maximum total changes (for percentage).
            max_additions: Maximum additions across all nodes.
            max_deletions: Maximum deletions across all nodes.

        Returns:
            Rich Text object with formatted label.
        """
        label = Text()

        # Node name
        name_color = "bold blue" if not node.is_file else "white"
        label.append(node.name, style=name_color)

        # Add spacing before stats
        label.append("  ")

        # Column 2-3: +/- counts
        if Column.COUNTS in self.config.columns:
            if node.additions > 0:
                label.append(f"+{node.additions}", style="green")
            label.append(" ")
            if node.deletions > 0:
                label.append(f"-{node.deletions}", style="red")
            label.append("  ")

        # Column 4-5: Progress bars with consistent breakpoint
        # Green bar grows RTL (right-to-left), red bar grows LTR (left-to-right)
        if Column.BARS in self.config.columns:
            dual_bar = DualProgressBar(
                additions=node.additions,
                deletions=node.deletions,
                max_additions=max_additions,
                max_deletions=max_deletions,
                width=self.config.bar_width,
            )
            label.append_text(dual_bar.to_text())
            label.append("  ")

        # Column 6: Percentage
        if Column.PERCENTAGES in self.config.columns and max_changes > 0:
            percentage = (node.total_changes / max_changes) * 100
            label.append(f"{percentage:5.1f}%", style="cyan")

        return label


# Backward compatibility alias (deprecated)
DiffTreeRenderer = DiffTree
