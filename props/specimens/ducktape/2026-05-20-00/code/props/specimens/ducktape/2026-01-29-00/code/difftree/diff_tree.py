"""Render tree structure with rich formatting and progress bars.

This module handles the VIEW layer - rendering TreeNode data with:
- Path collapsing for single-child directories
- Tree decoration styling (dim guides)
- Progress bars and statistics formatting
- Table layout and column configuration

Takes immutable TreeNode from tree.py and renders it to Rich console output.
"""

from io import StringIO

from rich.console import Console, ConsoleOptions, RenderResult
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from difftree.config import DEFAULT_CONFIG, Column, RenderConfig
from difftree.progress_bar import DEFAULT_LEFT_BLOCKS, DEFAULT_RIGHT_BLOCKS, ProgressBar
from difftree.tree import TreeNode


class DiffTree:
    """Renderable diff tree with progress bars and statistics."""

    def __init__(self, root: TreeNode, config: RenderConfig | None = None):
        self.root = root
        self.config = config or DEFAULT_CONFIG

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render as a Table with aligned tree structure and statistics."""
        # Get totals from root (which has aggregated stats from all children)
        total_additions = self.root.additions
        total_deletions = self.root.deletions
        total_changes = total_additions + total_deletions if (total_additions + total_deletions) > 0 else 1

        tree = self._build_tree_structure(self.root, depth=0)

        # Render tree to capture its visual structure
        # Use a temporary console that doesn't output to avoid double printing
        temp_output = StringIO()
        temp_console = Console(
            file=temp_output,
            width=options.max_width or 80,
            legacy_windows=False,
            force_terminal=True,  # Force ANSI codes to preserve styling
            color_system="standard" if console._color_system else None,  # Match parent console
        )
        temp_console.print(tree)

        # Parse the rendered output into lines
        tree_output = temp_output.getvalue()
        tree_lines = []
        for line in tree_output.rstrip("\n").split("\n"):
            tree_lines.append(Text.from_ansi(line))

        nodes_in_order = self._flatten_tree(self.root, depth=0)

        table = Table.grid(padding=0)  # No padding to avoid space between bars

        for column in self.config.columns:
            if column == Column.TREE:
                # Tree column takes remaining space after other columns
                # Use no_wrap and ellipsis to prevent line wrapping
                table.add_column(justify="left", overflow="ellipsis", no_wrap=True)
            elif column == Column.COUNTS:
                table.add_column(justify="right")  # Additions (right-aligned)
                table.add_column(justify="left")  # Deletions (left-aligned)
            elif column == Column.BARS:
                # Constrain bars to configured width so tree always has space
                # Use ratio for proportional distribution within bar_width constraint
                table.add_column(
                    justify="right",
                    ratio=total_additions if total_additions > 0 else 1,
                    max_width=self.config.bar_width,
                )
                table.add_column(
                    justify="left", ratio=total_deletions if total_deletions > 0 else 1, max_width=self.config.bar_width
                )
            elif column == Column.PERCENTAGES:
                table.add_column(justify="right")

        for tree_line, node in zip(tree_lines, nodes_in_order, strict=True):
            row: list[Text | ProgressBar] = []
            for column in self.config.columns:
                if column == Column.TREE:
                    # Add space after tree column
                    tree_with_space = Text.assemble(tree_line, " ")
                    row.append(tree_with_space)
                elif column == Column.COUNTS:
                    additions_cell, deletions_cell = self._make_count_cells(node)
                    # Add space after additions, before deletions
                    additions_with_space = Text.assemble(additions_cell, " ")
                    deletions_with_space = Text.assemble(" ", deletions_cell)
                    row.append(additions_with_space)
                    row.append(deletions_with_space)
                elif column == Column.BARS:
                    green_cell, red_cell = self._make_bar_cells(node, total_additions, total_deletions)
                    # No space between bar columns (they're rendered together)
                    # Add table columns with padding to create space before bars
                    row.append(green_cell)
                    row.append(red_cell)
                elif column == Column.PERCENTAGES:
                    # Space before percentage
                    pct_cell = self._make_percentage_cell(node, total_changes)
                    pct_with_space = Text.assemble(" ", pct_cell)
                    row.append(pct_with_space)
            table.add_row(*row)

        yield table

    def _should_recurse_into_children(self, node: TreeNode, depth: int) -> bool:
        """Check if we should recurse into a node's children.

        Returns False if:
        - Node is a file
        - Node has no children
        - We've exceeded max_depth
        """
        return (
            (self.config.max_depth is None or depth < self.config.max_depth)
            and not node.is_file
            and bool(node.children)
        )

    def _get_collapsed_path_and_node(self, node: TreeNode, depth: int) -> tuple[str, TreeNode, int]:
        """
        Get the collapsed path and final node for a single-child directory chain.

        Returns:
            Tuple of (collapsed_path, final_node, final_depth)
        """
        path_parts = []
        current = node
        current_depth = depth

        # Follow single-child chains until we hit a file, multi-child dir, or max depth
        while (
            not current.is_file
            and len(current.children) == 1
            and (self.config.max_depth is None or current_depth < self.config.max_depth)
        ):
            path_parts.append(current.name)
            current = next(iter(current.children.values()))
            current_depth += 1

        # Add the final node's name
        path_parts.append(current.name)

        collapsed_path = "/".join(path_parts)
        return collapsed_path, current, current_depth

    def _build_tree_structure(self, node: TreeNode, depth: int = 0) -> Tree:
        """Build Rich Tree with filenames only (no stats), collapsing single-child paths."""
        # Collect collapsed path for single-child directory chains
        collapsed_path, final_node, final_depth = self._get_collapsed_path_and_node(node, depth)

        # Directories in bold blue, files in default color
        name_color = "bold blue" if not final_node.is_file else ""
        label = Text(collapsed_path, style=name_color, overflow="ellipsis")
        tree = Tree(label, guide_style="dim")

        if self._should_recurse_into_children(final_node, final_depth):
            for child in final_node.children.values():
                child_tree = self._build_tree_structure(child, final_depth + 1)
                tree.add(child_tree)

        return tree

    def _flatten_tree(self, node: TreeNode, depth: int = 0) -> list[TreeNode]:
        """Flatten tree into list of nodes in render order, matching collapsed paths."""
        # Use the same collapsing logic as _build_tree_structure
        _, final_node, final_depth = self._get_collapsed_path_and_node(node, depth)

        result = [final_node]

        if self._should_recurse_into_children(final_node, final_depth):
            for child in final_node.children.values():
                result.extend(self._flatten_tree(child, final_depth + 1))

        return result

    def _make_count_cells(self, node: TreeNode) -> tuple[Text, Text]:
        """Create count cells with additions (right-aligned) and deletions (left-aligned)."""
        if node.is_binary:
            return Text("[Binary]", style="dim"), Text("")

        additions_cell = Text()
        if node.additions > 0:
            additions_cell.append(f"+{node.additions}", style="green")

        deletions_cell = Text()
        if node.deletions > 0:
            deletions_cell.append(f"-{node.deletions}", style="red")

        return additions_cell, deletions_cell

    def _make_bar_cells(
        self, node: TreeNode, total_additions: int, total_deletions: int
    ) -> tuple[ProgressBar, ProgressBar]:
        """Create bar cells (green and red progress bars)."""
        if node.is_binary:
            # Return empty progress bars for binary files
            empty_green = ProgressBar(
                value=0,
                max_value=1,
                blocks=self.config.bar_right_blocks or DEFAULT_RIGHT_BLOCKS,
                align="right",
                style="green",
                max_width=0,
            )
            empty_red = ProgressBar(
                value=0,
                max_value=1,
                blocks=self.config.bar_left_blocks or DEFAULT_LEFT_BLOCKS,
                align="left",
                style="red",
                max_width=0,
            )
            return empty_green, empty_red

        # Scale bars independently to maintain equal proportional scale:
        # - Green bar scaled by total_additions (1 block = same number of added lines across all files)
        # - Red bar scaled by total_deletions (1 block = same number of deleted lines across all files)
        # Column widths are already proportional via ratio parameter, so 1 block represents
        # the same number of line changes in both addition and deletion bars.
        # No max_width on ProgressBars - let them expand to fill their proportionally-sized columns.
        green_bar = ProgressBar(
            value=node.additions,
            max_value=total_additions if total_additions > 0 else 1,
            blocks=self.config.bar_right_blocks or DEFAULT_RIGHT_BLOCKS,
            align="right",
            style="green",
        )
        red_bar = ProgressBar(
            value=node.deletions,
            max_value=total_deletions if total_deletions > 0 else 1,
            blocks=self.config.bar_left_blocks or DEFAULT_LEFT_BLOCKS,
            align="left",
            style="red",
        )

        return green_bar, red_bar

    def _make_percentage_cell(self, node: TreeNode, max_changes: int) -> Text:
        """Create percentage cell showing relative change size."""
        if node.is_binary or max_changes == 0:
            return Text("")

        ratio = node.total_changes / max_changes
        return Text(f"{ratio:>6.1%}", style="cyan")
