"""Render tree structure with rich formatting and progress bars."""

from typing import Optional

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

from .config import RenderConfig
from .tree import TreeNode

# Unicode block characters for progress bars (from empty to full)
BLOCKS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]


class DiffTreeRenderer:
    """Renders a diff tree with progress bars and statistics."""

    def __init__(
        self,
        config: Optional[RenderConfig] = None,
        console: Optional[Console] = None,
    ):
        """
        Initialize the renderer.

        Args:
            config: RenderConfig object (uses default if None).
            console: Rich Console instance (creates one if None).
        """
        self.console = console or Console()
        self.config = config or RenderConfig.default()

    def render(self, root: TreeNode, max_depth: Optional[int] = None) -> None:
        """
        Render the tree to the console.

        Args:
            root: Root TreeNode to render.
            max_depth: Maximum depth to render (None for unlimited).
        """
        # Calculate max values for scaling progress bars
        max_changes = root.total_changes if root.total_changes > 0 else 1

        # Build Rich Tree and render
        tree = self._build_rich_tree(root, max_changes, max_depth, depth=0)
        self.console.print(tree)

    def _build_rich_tree(
        self,
        node: TreeNode,
        max_changes: int,
        max_depth: Optional[int],
        depth: int = 0,
    ) -> Tree:
        """
        Build a Rich Tree from a TreeNode.

        Args:
            node: TreeNode to convert.
            max_changes: Maximum changes for scaling progress bars.
            max_depth: Maximum depth to render (None for unlimited).
            depth: Current depth in tree.

        Returns:
            Rich Tree object.
        """
        # Create label for this node
        label = self._make_node_label(node, max_changes)

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
                    max_depth,
                    depth + 1,
                )
                tree.add(child_tree)

        return tree

    def _make_node_label(self, node: TreeNode, max_changes: int) -> Text:
        """
        Create the label for a tree node with stats and progress bars.

        Args:
            node: TreeNode to create label for.
            max_changes: Maximum changes for scaling progress bars.

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
        if self.config.show_counts():
            if node.additions > 0:
                label.append(f"+{node.additions}", style="green")
            label.append(" ")
            if node.deletions > 0:
                label.append(f"-{node.deletions}", style="red")
            label.append("  ")

        # Column 4-5: Progress bars
        if self.config.show_bars():
            add_bar = self._make_progress_bar(
                node.additions,
                max_changes,
                self.config.bar_width,
                align="right",
                color="green",
            )
            del_bar = self._make_progress_bar(
                node.deletions,
                max_changes,
                self.config.bar_width,
                align="left",
                color="red",
            )
            label.append(add_bar)
            label.append(del_bar)
            label.append("  ")

        # Column 6: Percentage
        if self.config.show_percentages() and max_changes > 0:
            percentage = (node.total_changes / max_changes) * 100
            label.append(f"{percentage:5.1f}%", style="cyan")

        return label

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
        ratio = 0 if max_value == 0 else min(value / max_value, 1.0)

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
