"""Progress bar renderables with RTL/LTR alignment support."""

from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

# Unicode block characters for progress bars (from empty to full)
# Same as Rich's END_BLOCK_ELEMENTS plus full block
BLOCKS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]


class ProgressBar:
    """A single progress bar with RTL or LTR alignment.

    Unlike Rich's built-in Bar, this supports:
    - Right-to-left (RTL) fill direction
    - Minimum sliver for values >0
    - Designed for diff statistics visualization

    Args:
        value: Current value (e.g., number of additions).
        max_value: Maximum value for scaling (e.g., max additions across files).
        width: Width in characters.
        align: Fill direction - "left" (LTR) or "right" (RTL).
        style: Rich style for the bar (e.g., "green", "red").
    """

    def __init__(
        self,
        value: int,
        max_value: int,
        width: int = 20,
        align: Literal["left", "right"] = "left",
        style: str = "default",
    ):
        self.value = value
        self.max_value = max_value
        self.width = width
        self.align = align
        self.style = style

    def to_text(self) -> Text:
        """Render the progress bar as a Text object."""
        ratio = 0 if self.max_value == 0 else min(self.value / self.max_value, 1.0)

        # Calculate how many characters to fill
        filled_width = ratio * self.width
        full_blocks = int(filled_width)
        partial_block_index = int((filled_width - full_blocks) * (len(BLOCKS) - 1))

        # Build the bar
        bar_chars = BLOCKS[-1] * full_blocks
        if full_blocks < self.width and partial_block_index > 0:
            bar_chars += BLOCKS[partial_block_index]

        # Ensure any value >0 shows at least a minimal sliver
        if self.value > 0 and not bar_chars:
            bar_chars = BLOCKS[1]  # Smallest visible block: ▏

        # Pad to full width based on alignment
        if self.align == "right":
            bar_chars = bar_chars.rjust(self.width)
        else:
            bar_chars = bar_chars.ljust(self.width)

        return Text(bar_chars, style=self.style)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the progress bar."""
        yield self.to_text()


class DualProgressBar:
    """Twin progress bars: additions (RTL, green) + deletions (LTR, red).

    Renders two progress bars side-by-side with no gap:
    - Left bar: additions, right-aligned (fills RTL), green
    - Right bar: deletions, left-aligned (fills LTR), red

    This creates a visual "breakpoint" where the bars meet, showing
    the relative proportions of additions vs deletions.

    Args:
        additions: Number of added lines.
        deletions: Number of deleted lines.
        max_additions: Maximum additions across all files.
        max_deletions: Maximum deletions across all files.
        width: Width of each bar in characters (total width = 2 * width).
    """

    def __init__(
        self,
        additions: int,
        deletions: int,
        max_additions: int,
        max_deletions: int,
        width: int = 20,
    ):
        self.additions = additions
        self.deletions = deletions
        self.max_additions = max_additions
        self.max_deletions = max_deletions
        self.width = width

    def to_text(self) -> Text:
        """Render the dual progress bar as a Text object."""
        # Create RTL bar for additions (green, right-aligned)
        add_bar = ProgressBar(
            self.additions,
            self.max_additions,
            width=self.width,
            align="right",
            style="green",
        )

        # Create LTR bar for deletions (red, left-aligned)
        del_bar = ProgressBar(
            self.deletions,
            self.max_deletions,
            width=self.width,
            align="left",
            style="red",
        )

        # Combine the two bars
        result = Text()
        result.append_text(add_bar.to_text())
        result.append_text(del_bar.to_text())
        return result

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the dual progress bar."""
        yield self.to_text()
