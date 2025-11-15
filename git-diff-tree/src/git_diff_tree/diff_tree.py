"""Render tree structure with rich formatting and progress bars."""

from typing import Optional

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .config import Column, RenderConfig
from .progress_bar import ProgressBar
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

        Strategy: Render tree structure to temp console, then build a Table
        where the first column contains the tree lines and subsequent columns
        contain stats. This ensures all stats align at the same column position
        regardless of filename length or nesting depth.

        Args:
            console: The console instance.
            options: Console rendering options.

        Yields:
            Rich Table with aligned columns.
        """
        # Find max additions and deletions for scaling
        max_additions, max_deletions = self._find_max_additions_deletions(self.root)
        max_changes = (
            max_additions + max_deletions if (max_additions + max_deletions) > 0 else 1
        )

        # Step 1: Build Rich Tree with ONLY filenames (no stats)
        tree = self._build_tree_structure(self.root, depth=0)

        # Step 2: Render tree to temporary console to get tree structure lines
        from rich.segment import Segment

        temp_console = Console(record=True, width=options.max_width or 80)
        temp_console.print(tree)
        segments = temp_console._record_buffer
        lines = list(Segment.split_lines(segments))

        # Convert segment lines to Text objects
        tree_lines = []
        for line_segments in lines:
            text = Text()
            for segment in line_segments:
                if segment.text:
                    text.append(segment.text, style=segment.style)
            tree_lines.append(text)

        # Step 3: Flatten tree to get nodes in same order as rendered lines
        nodes_in_order = self._flatten_tree(self.root, depth=0)

        # Step 4: Build Table with tree column + stat columns
        table = Table.grid(padding=0)
        table.add_column(justify="left")  # Tree structure + filename

        if Column.COUNTS in self.config.columns:
            table.add_column(justify="right")  # Counts

        if Column.BARS in self.config.columns:
            table.add_column(justify="right")  # Green bar (RTL)
            table.add_column(justify="left")  # Red bar (LTR)

        if Column.PERCENTAGES in self.config.columns:
            table.add_column(justify="right")  # Percentage

        # Step 5: Add rows pairing tree lines with node stats
        for tree_line, node in zip(tree_lines, nodes_in_order):
            row = [tree_line]
            row.extend(
                self._make_stat_cells(node, max_changes, max_additions, max_deletions)
            )
            table.add_row(*row)

        yield table

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

    def _build_tree_structure(
        self,
        node: TreeNode,
        depth: int = 0,
    ) -> Tree:
        """
        Build a Rich Tree with ONLY filenames (no stats).

        This is rendered to a temp console to extract tree structure lines.

        Args:
            node: TreeNode to convert.
            depth: Current depth in tree.

        Returns:
            Rich Tree object with only filenames.
        """
        # Create label with ONLY the filename (colored)
        name_color = "bold blue" if not node.is_file else "white"
        label = Text(node.name, style=name_color)

        # Create Rich Tree with the label
        tree = Tree(label)

        # Add children if within depth limit
        if (
            (self.config.max_depth is None or depth < self.config.max_depth)
            and not node.is_file
            and node.children
        ):
            for child in node.children.values():
                child_tree = self._build_tree_structure(child, depth + 1)
                tree.add(child_tree)

        return tree

    def _flatten_tree(
        self,
        node: TreeNode,
        depth: int = 0,
    ) -> list[TreeNode]:
        """
        Flatten tree into a list of nodes in render order.

        This matches the order that Rich's Tree renders lines.

        Args:
            node: TreeNode to flatten.
            depth: Current depth in tree.

        Returns:
            List of TreeNodes in render order.
        """
        result = [node]

        # Add children if within depth limit
        if (
            (self.config.max_depth is None or depth < self.config.max_depth)
            and not node.is_file
            and node.children
        ):
            for child in node.children.values():
                result.extend(self._flatten_tree(child, depth + 1))

        return result

    def _make_stat_cells(
        self, node: TreeNode, max_changes: int, max_additions: int, max_deletions: int
    ) -> list[Text]:
        """
        Create stat cells for a tree node (counts, bars, percentage).

        These cells are added to the table row after the tree structure cell.

        Args:
            node: TreeNode to create stats for.
            max_changes: Maximum total changes (for percentage).
            max_additions: Maximum additions across all nodes.
            max_deletions: Maximum deletions across all nodes.

        Returns:
            List of Text/Renderable objects for table cells.
        """
        cells = []

        # Cell: Counts
        if Column.COUNTS in self.config.columns:
            counts = Text("  ")  # Spacing
            if node.additions > 0:
                counts.append(f"+{node.additions}", style="green")
            counts.append(" ")
            if node.deletions > 0:
                counts.append(f"-{node.deletions}", style="red")
            cells.append(counts)

        # Cells: Progress bars (two separate cells for alignment)
        if Column.BARS in self.config.columns:
            # Green bar (RTL - additions)
            green_bar = ProgressBar(
                value=node.additions,
                max_value=max_additions,
                width=self.config.bar_width,
                align="right",
                style="green",
            )

            # Red bar (LTR - deletions)
            red_bar = ProgressBar(
                value=node.deletions,
                max_value=max_deletions,
                width=self.config.bar_width,
                align="left",
                style="red",
            )

            # Add spacing before green bar
            green_cell = Text("  ")
            green_cell.append_text(green_bar.to_text())
            cells.append(green_cell)
            cells.append(red_bar.to_text())

        # Cell: Percentage
        if Column.PERCENTAGES in self.config.columns:
            if max_changes > 0:
                percentage = (node.total_changes / max_changes) * 100
                pct_text = Text("  ")  # Spacing
                pct_text.append(f"{percentage:5.1f}%", style="cyan")
                cells.append(pct_text)
            else:
                cells.append(Text("  "))

        return cells


# Backward compatibility alias (deprecated)
DiffTreeRenderer = DiffTree
