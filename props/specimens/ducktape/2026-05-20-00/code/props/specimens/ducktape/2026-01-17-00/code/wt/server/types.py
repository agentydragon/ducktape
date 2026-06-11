from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..shared.protocol import WorktreeID


@dataclass(frozen=True, slots=True)
class GitWorkingStatus:
    """Server-internal typed status from gitstatusd.

    We do not track filenames (gitstatusd doesn't return them by default),
    only counts and freshness metadata.
    """

    dirty_count: int
    untracked_count: int
    updated_at: datetime | None
    has_cache: bool


def _now() -> datetime:
    return datetime.now()


@dataclass(frozen=True, slots=True)
class DiscoveredWorktree:
    """Filesystem-discovered worktree instance (daemon-internal).

    wtid must be provided by the minting point; do not derive here.
    """

    path: Path
    name: str
    wtid: WorktreeID
    discovered_at: datetime = field(default_factory=_now, compare=False)
    last_seen: datetime = field(default_factory=_now, compare=False)
