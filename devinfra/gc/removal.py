"""The outcome of removing one candidate, shared by every domain's removal step."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Removed[S]:
    subject: S


@dataclass(frozen=True, slots=True)
class Skipped[S]:
    subject: S
    reason: str  # revalidation found the candidate no longer qualifies


@dataclass(frozen=True, slots=True)
class Failed[S]:
    subject: S
    error: str
    leftover: Path | None = None  # where the tree sits if it was moved aside (quarantine, trash) but not deleted


type RemovalResult[S] = Removed[S] | Skipped[S] | Failed[S]
