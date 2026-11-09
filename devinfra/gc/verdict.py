"""The PRUNE/KEEP/REVIEW verdict every domain's classification types share."""

from __future__ import annotations

from devinfra.gc.scan_progress import ProgressCategory


class Prunable:
    """Marks a classification that may be removed."""

    __slots__ = ()


class Retained:
    """Marks a classification that must stay."""

    __slots__ = ()


class Review:
    """Marks a classification needing a human decision."""

    __slots__ = ()


def verdict_of(item: Prunable | Retained | Review) -> ProgressCategory:
    if isinstance(item, Prunable):
        return "PRUNE"
    if isinstance(item, Retained):
        return "KEEP"
    return "REVIEW"
