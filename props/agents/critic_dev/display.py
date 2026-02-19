"""Display utilities for critic-dev CLI commands."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rich import box
from rich.table import Table

SHORT_SHA_LENGTH = 6

JustifyMethod = Literal["left", "right", "center", "full", "default"]


def short_sha(sha: str) -> str:
    """Return first 6 characters of SHA hash for display."""
    return sha[:SHORT_SHA_LENGTH]


def fmt_pct(value: float | None) -> str:
    """Format value as percentage (1 decimal place) or dash if None."""
    return f"{value:.1%}" if value is not None else "—"


def fmt_float(value: float | None, decimals: int = 2) -> str:
    """Format float with specified decimal places, or dash if None."""
    return f"{value:.{decimals}f}" if value is not None else "—"


def fmt_model(model: str, max_length: int = 12) -> str:
    """Truncate model name for display."""
    return model[:max_length]


@dataclass
class ColumnDef[T, V]:
    """Column definition for declarative table building."""

    name: str
    accessor: Callable[[T], V]
    formatter: Callable[[V], str] = str
    width: int | None = None
    justify: JustifyMethod = "left"
    style: str | None = None


def build_table_from_schema[T](
    rows: Sequence[T],
    columns: Sequence[ColumnDef[T, Any]],
    *,
    show_header: bool = True,
    box_style: box.Box = box.SIMPLE,
) -> Table:
    """Build a Rich table from column schema and data rows."""
    table = Table(show_header=show_header, header_style="bold cyan", box=box_style)

    # Add columns from schema
    for col in columns:
        table.add_column(col.name, width=col.width, justify=col.justify, style=col.style)

    # Add rows
    for row in rows:
        values = [col.formatter(col.accessor(row)) for col in columns]
        table.add_row(*values)

    return table
