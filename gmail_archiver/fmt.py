"""Formatting helpers for truncated collections."""

from __future__ import annotations


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
