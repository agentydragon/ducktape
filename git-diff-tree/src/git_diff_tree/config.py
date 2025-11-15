"""Configuration for git-diff-tree rendering."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class Column(StrEnum):
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
