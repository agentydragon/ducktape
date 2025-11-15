"""Configuration for git-diff-tree rendering."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Column(str, Enum):
    """Available columns to display."""

    TREE = "tree"
    COUNTS = "counts"
    BARS = "bars"
    PERCENTAGES = "percentages"


@dataclass
class RenderConfig:
    """Configuration for rendering diff trees."""

    columns: list[Column]
    bar_width: int = 20
    sort_by: str = "size"
    max_depth: Optional[int] = None

    def show_tree(self) -> bool:
        """Whether to show tree column."""
        return Column.TREE in self.columns

    def show_counts(self) -> bool:
        """Whether to show counts column."""
        return Column.COUNTS in self.columns

    def show_bars(self) -> bool:
        """Whether to show progress bars column."""
        return Column.BARS in self.columns

    def show_percentages(self) -> bool:
        """Whether to show percentages column."""
        return Column.PERCENTAGES in self.columns

    @classmethod
    def default(cls) -> "RenderConfig":
        """Create default configuration with all columns."""
        return cls(
            columns=[
                Column.TREE,
                Column.COUNTS,
                Column.BARS,
                Column.PERCENTAGES,
            ]
        )

    @classmethod
    def minimal(cls) -> "RenderConfig":
        """Create minimal configuration with only tree."""
        return cls(columns=[Column.TREE])
