from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .types import DiscoveredWorktree


@dataclass
class ChangeSet:
    added: list[DiscoveredWorktree] = field(default_factory=list)
    removed: list[DiscoveredWorktree] = field(default_factory=list)
    unchanged: list[DiscoveredWorktree] = field(default_factory=list)


class WorktreeRegistry:
    def __init__(self) -> None:
        self._known: dict[Path, DiscoveredWorktree] = {}

    @property
    def known(self) -> dict[Path, DiscoveredWorktree]:
        return self._known

    def apply(self, current: Iterable[DiscoveredWorktree]) -> ChangeSet:
        cur_map = {wt.path: wt for wt in current}
        added = [cur_map[p] for p in cur_map.keys() - self._known.keys()]
        removed = [self._known[p] for p in self._known.keys() - cur_map.keys()]
        unchanged = [self._known[p] for p in self._known.keys() & cur_map.keys()]
        # Update known with current
        self._known = cur_map
        return ChangeSet(added=added, removed=removed, unchanged=unchanged)
