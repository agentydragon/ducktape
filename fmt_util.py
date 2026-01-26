"""Formatting utilities for displaying truncated collections.

Provides generic functions for formatting collections with overflow indicators,
used across multiple packages (props, tools/ci, gmail_archiver, etc.).
"""

from __future__ import annotations

from collections.abc import Sequence


def format_limited_list(
    items: Sequence[str], limit: int, *, separator: str = ", ", overflow_format: str = " (+{remaining} more)"
) -> str:
    """Join items with separator, adding overflow indicator if truncated.

    Args:
        items: Items to join
        limit: Maximum number of items to show
        separator: String between items
        overflow_format: Format string with {remaining} placeholder

    Returns:
        Joined string like "a, b, c (+5 more)" or just "a, b, c" if not truncated

    Examples:
        >>> format_limited_list(["a", "b", "c", "d", "e"], 3)
        'a, b, c (+2 more)'
        >>> format_limited_list(["a", "b"], 3)
        'a, b'
        >>> format_limited_list(["x", "y", "z"], 2, overflow_format=" ... and {remaining} more")
        'x, y ... and 1 more'
    """
    if len(items) <= limit:
        return separator.join(items)
    shown = items[:limit]
    remaining = len(items) - limit
    return separator.join(shown) + overflow_format.format(remaining=remaining)


def format_truncation_suffix(total: int, shown: int, item_name: str = "") -> str:
    """Return '... and N more {item_name}' or empty string if all shown.

    Use this when displaying items one-by-one with a footer for overflow.

    Args:
        total: Total number of items
        shown: Number of items shown
        item_name: Optional name for items (e.g., "files", "errors")

    Examples:
        >>> format_truncation_suffix(10, 5, "files")
        '... and 5 more files'
        >>> format_truncation_suffix(10, 5)
        '... and 5 more'
        >>> format_truncation_suffix(3, 5)
        ''
    """
    if total <= shown:
        return ""
    remaining = total - shown
    if item_name:
        return f"... and {remaining} more {item_name}"
    return f"... and {remaining} more"
