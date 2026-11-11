"""Progress sink shared by the workspace scanners and their CLI renderer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

ProgressCategory = Literal["PRUNE", "KEEP", "REVIEW"]


@dataclass(frozen=True, slots=True)
class ProgressPhase:
    """Read-only progress for one dynamically discovered kind of work."""

    discovered: int
    ready: int
    active: int
    completed: int
    outcomes: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", MappingProxyType(dict(self.outcomes)))


@dataclass(frozen=True, slots=True)
class ProgressState:
    """A point-in-time view of work across phases."""

    phases: Mapping[str, ProgressPhase]
    waiting: int
    discovering: bool
    stopped: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", MappingProxyType(dict(self.phases)))


class ProgressSink(Protocol):
    """Mutable scan progress state, updated directly by each classifier."""

    def start_phase(self, phase: str, total: int) -> None: ...

    def record(self, phase: str, category: ProgressCategory) -> None:
        """Record one finished item; implementations must support concurrent calls."""
        ...


class NullProgress:
    """Progress sink for library callers that do not need live reporting."""

    def start_phase(self, phase: str, total: int) -> None:
        pass

    def record(self, phase: str, category: ProgressCategory) -> None:
        pass


NULL_PROGRESS: ProgressSink = NullProgress()
