"""Progress bar renderables with RTL/LTR alignment support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.text import Text


@dataclass(frozen=True)
class BlockChars:
    """Configuration for progress bar block characters.

    Represents the characters used to render progress bars with different fill levels:
    - full: Completely filled block
    - empty: Empty space
    - partials: Progressively fuller partial blocks
    """

    full: str
    empty: str = " "
    partials: tuple[str, ...] = ("▏", "▎", "▍", "▌", "▋", "▊", "▉")

    def __post_init__(self):
        """Validate block character configuration."""
        if len(self.partials) < 1:
            raise ValueError("partials must have at least 1 element")

    @classmethod
    def simple(cls, char: str, empty: str = " ") -> BlockChars:
        """Create block chars using single character for all fill levels."""
        return cls(full=char, empty=empty, partials=(char,))


# Default block character configurations
# Note: LTR (left-to-right) and RTL (right-to-left) alignments flip which side
# gets the filled blocks vs empty space:
# - LTR: filled blocks on left, empty space on right (e.g., "███    ")
# - RTL: empty space on left, filled blocks on right (e.g., "    ███")

# Left-growing blocks (for LTR alignment): filled portion grows from left edge
DEFAULT_LEFT_BLOCKS = BlockChars(full="█", empty=" ", partials=("▏", "▎", "▍", "▌", "▋", "▊", "▉"))

# Right-growing blocks (for RTL alignment): filled portion grows from right edge
# Note: Unicode has limited right-block granularity, so we approximate
DEFAULT_RIGHT_BLOCKS = BlockChars(full="█", empty=" ", partials=("▕", "▕", "▐", "▐", "▐", "▉", "█"))


class ProgressBar:
    """Progress bar with RTL or LTR alignment for diff statistics."""

    def __init__(
        self,
        value: int,
        max_value: int,
        blocks: BlockChars,
        align: Literal["left", "right"] = "left",
        style: str = "default",
        max_width: int | None = None,
        min_width: int = 5,
    ):
        self.value = value
        self.max_value = max_value
        self.align = align
        self.style = style
        self.blocks = blocks
        self.max_width = max_width
        self.min_width = min_width

    def _render_bar(self, width: int) -> Text:
        """Render the progress bar at a specific width."""
        ratio = 0 if self.max_value == 0 else min(self.value / self.max_value, 1.0)
        filled_width = ratio * width
        full_blocks = int(filled_width)

        # Calculate partial block index
        # The fractional part (0.0-1.0) is divided into (num_partials + 1) buckets:
        # - Bucket 0: no partial (empty)
        # - Buckets 1 to num_partials: use partials[0] through partials[num_partials-1]
        num_partials = len(self.blocks.partials)
        partial_block_index = int((filled_width - full_blocks) * (num_partials + 1))

        # Build bar components
        full_part = self.blocks.full * full_blocks
        partial_part = ""
        if full_blocks < width and partial_block_index > 0:
            partial_part = self.blocks.partials[min(partial_block_index - 1, num_partials - 1)]

        # Ensure minimum sliver for non-zero values
        if self.value > 0 and not full_part and not partial_part:
            partial_part = self.blocks.partials[0]

        # Combine parts and justify based on alignment
        if self.align == "right":
            # RTL: partial then full, right-justified
            bar_chars = (partial_part + full_part).rjust(width, self.blocks.empty)
        else:
            # LTR: full then partial, left-justified
            bar_chars = (full_part + partial_part).ljust(width, self.blocks.empty)

        return Text(bar_chars, style=self.style)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        if self.max_width is not None:
            width = min(width, self.max_width)
        width = max(width, self.min_width)
        yield self._render_bar(width)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        max_width = self.max_width if self.max_width is not None else options.max_width
        return Measurement(self.min_width, max_width)
