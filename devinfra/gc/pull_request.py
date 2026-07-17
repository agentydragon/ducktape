"""The pull-request model shared by the worktree and branch classifiers.

Network-free: `workspace_gc` fetches PR state from GitHub and injects it as `dict[str,
PrInfo]`, so the classifiers stay offline-testable.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class PrState(enum.StrEnum):
    MERGED = "merged"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class PrInfo:
    number: int
    state: PrState
    head_sha: str | None = None  # tip the PR merged; lets branch_gc prune squash-merges


def pr_phrase(pr: PrInfo) -> str:
    match pr.state:
        case PrState.OPEN:
            return f"open PR #{pr.number}"
        case PrState.MERGED:
            return f"PR #{pr.number} merged"
        case PrState.CLOSED:
            return f"closed PR #{pr.number}"
