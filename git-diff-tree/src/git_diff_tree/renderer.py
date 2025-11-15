"""Render tree structure with rich formatting and progress bars."""

from typing import List, Optional

from rich.console import Console
from rich.text import Text

from .tree import TreeNode


# Unicode block characters for progress bars (from empty to full)
BLOCKS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]


class DiffTreeRenderer:
    """Renders a diff tree with progress bars and statistics."""

    def __init__(
        self,
        console: Optional[Console] = None,
        show_counts: bool = True,
        show_bars: bool = True,
        show_percentages: bool = True,
        bar_width: int = 20,
    ):
        """
        Initialize the renderer.

        Args:
            console: Rich Console instance (creates one if None).
            show_counts: Show +/- count columns.
            show_bars: Show progress bar columns.
            show_percentages: Show percentage column.
            bar_width: Width of each progress bar in characters.
        """
        self.console = console or Console()
        self.show_counts = show_counts
        self.show_bars = show_bars
        self.show_percentages = show_percentages
        self.bar_width = bar_width

    def render(self, root: TreeNode, max_depth: Optional[int] = None) -> None:
        """
        Render the tree to the console.

        Args:
            root: Root TreeNode to render.
            max_depth: Maximum depth to render (None for unlimited).
        """
        # Calculate max values for scaling progress bars
        max_changes = root.total_changes if root.total_changes > 0 else 1

        # Render tree recursively
        lines: List[Text] = []
        self._render_node(
            root,
            lines=lines,
            prefix="",
            is_last=True,
            depth=0,
            max_depth=max_depth,
            max_changes=max_changes,
        )

        # Print all lines
        for line in lines:
            self.console.print(line, overflow="ignore", no_wrap=True)

    def _render_node(
        self,
        node: TreeNode,
        lines: List[Text],
        prefix: str,
        is_last: bool,
        depth: int,
        max_depth: Optional[int],
        max_changes: int,
    ) -> None:
        """Recursively render a tree node and its children."""
        if max_depth is not None and depth > max_depth:
            return

        # Build the tree structure prefix
        if depth == 0:
            tree_prefix = ""
            connector = ""
        else:
            connector = "└── " if is_last else "├── "
            tree_prefix = prefix

        # Create the line
        line = Text()

        # Column 1: Tree structure + name
        name_color = "bold blue" if not node.is_file else "white"
        line.append(tree_prefix + connector, style="dim")
        line.append(node.name, style=name_color)

        # Add spacing before stats
        line.append("  ")

        # Column 2-3: +/- counts
        if self.show_counts:
            if node.additions > 0:
                line.append(f"+{node.additions}", style="green")
            line.append(" ")
            if node.deletions > 0:
                line.append(f"-{node.deletions}", style="red")
            line.append("  ")

        # Column 4-5: Progress bars
        if self.show_bars:
            add_bar = self._make_progress_bar(
                node.additions,
                max_changes,
                self.bar_width,
                align="right",
                color="green",
            )
            del_bar = self._make_progress_bar(
                node.deletions,
                max_changes,
                self.bar_width,
                align="left",
                color="red",
            )
            line.append(add_bar)
            line.append(del_bar)
            line.append("  ")

        # Column 6: Percentage
        if self.show_percentages and max_changes > 0:
            percentage = (node.total_changes / max_changes) * 100
            line.append(f"{percentage:5.1f}%", style="cyan")

        lines.append(line)

        # Render children
        if not node.is_file and node.children:
            children = list(node.children.values())
            for i, child in enumerate(children):
                is_last_child = i == len(children) - 1
                extension = "    " if is_last else "│   "
                new_prefix = prefix + extension if depth > 0 else ""

                self._render_node(
                    child,
                    lines=lines,
                    prefix=new_prefix,
                    is_last=is_last_child,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_changes=max_changes,
                )

    def _make_progress_bar(
        self,
        value: int,
        max_value: int,
        width: int,
        align: str,
        color: str,
    ) -> Text:
        """
        Create a Unicode block progress bar.

        Args:
            value: Current value.
            max_value: Maximum value for scaling.
            width: Width in characters.
            align: "left" or "right" alignment.
            color: Color style for the bar.

        Returns:
            Rich Text object with the progress bar.
        """
        if max_value == 0:
            ratio = 0
        else:
            ratio = min(value / max_value, 1.0)

        # Calculate how many characters to fill
        filled_width = ratio * width
        full_blocks = int(filled_width)
        partial_block_index = int((filled_width - full_blocks) * (len(BLOCKS) - 1))

        # Build the bar
        bar_chars = BLOCKS[-1] * full_blocks
        if full_blocks < width and partial_block_index > 0:
            bar_chars += BLOCKS[partial_block_index]

        # Ensure any value >0 shows at least a minimal sliver
        if value > 0 and not bar_chars:
            bar_chars = BLOCKS[1]  # Smallest visible block: ▏

        # Pad to full width
        if align == "right":
            bar_chars = bar_chars.rjust(width)
        else:
            bar_chars = bar_chars.ljust(width)

        return Text(bar_chars, style=color)
