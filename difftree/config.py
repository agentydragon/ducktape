"""Configuration for difftree rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from difftree.progress_bar import BlockChars


class Column(StrEnum):
    """Available columns to display."""

    TREE = "tree"
    COUNTS = "counts"
    BARS = "bars"
    PERCENTAGES = "percentages"


class SortMode(StrEnum):
    """Sort mode for tree nodes."""

    SIZE = "size"
    ALPHA = "alpha"


def parse_columns(columns_str: str) -> list[Column]:
    """Parse comma-separated column names into Column enum values."""
    column_list = []
    for col in columns_str.split(","):
        col_stripped = col.strip()
        try:
            column_list.append(Column(col_stripped.lower()))
        except ValueError:
            valid_options = ", ".join(c.value for c in Column)
            raise ValueError(f"Unknown column '{col_stripped}'. Valid options: {valid_options}") from None
    return column_list


@dataclass
class RenderConfig:
    """Rendering configuration for diff trees."""

    columns: list[Column]
    bar_width: int = 20
    sort_by: SortMode = SortMode.SIZE
    max_depth: int | None = None
    bar_left_blocks: BlockChars | None = None
    bar_right_blocks: BlockChars | None = None


# Default configuration with all columns
DEFAULT_CONFIG = RenderConfig(columns=[Column.TREE, Column.COUNTS, Column.BARS, Column.PERCENTAGES])
