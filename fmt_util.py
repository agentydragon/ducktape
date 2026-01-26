"""Formatting utilities for displaying truncated collections.

Provides generic functions for formatting collections with overflow indicators,
used across multiple packages (props, tools/ci, gmail_archiver, etc.).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence


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


def log_truncated(
    logger: logging.Logger,
    label: str,
    items: Collection[str],
    limit: int = 20,
    *,
    separator: str = ", ",
    overflow_format: str = " ... and {remaining} more",
) -> None:
    """Log a collection with truncation, using format_limited_list.

    Args:
        logger: Logger instance to use
        label: Label prefix for the log line
        items: Items to log
        limit: Maximum number of items to show
        separator: String between items
        overflow_format: Format string with {remaining} placeholder

    Example:
        log_truncated(logger, "Changed files", files, 10)
        # Logs: "Changed files: a.py, b.py, c.py ... and 7 more"
    """
    items_list = list(items)
    formatted = format_limited_list(items_list, limit, separator=separator, overflow_format=overflow_format)
    logger.info("%s: %s", label, formatted)
