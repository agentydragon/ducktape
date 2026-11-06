"""Formatting helpers for truncated collections."""

from __future__ import annotations


def format_truncation_suffix(total: int, shown: int, item_name: str = "") -> str:
    """Return "... and N more {item_name}" or empty when not truncated."""
    if total <= shown:
        return ""
    remaining = total - shown
    if item_name:
        return f"... and {remaining} more {item_name}"
    return f"... and {remaining} more"
