"""The pull-request model shared by the worktree and branch classifiers.

Network-free: `workspace_gc` fetches PR state from GitHub and injects it as `dict[str,
PrInfo]`, so the classifiers stay offline-testable.
"""

from __future__ import annotations

import enum
from collections.abc import Collection
from dataclasses import dataclass


class PrState(enum.StrEnum):
    """GitHub's `PullRequestState`, spelled as the GraphQL API returns it."""

    MERGED = "MERGED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class PrInfo:
    number: int
    state: PrState
    head_sha: str | None = None  # tip the PR merged; lets branch_gc classify squash-merge candidates


@dataclass(frozen=True, slots=True)
class PrLookup:
    """A missing state means no PR only when the branch is not unknown."""

    states: dict[str, PrInfo]
    unknown: Collection[str] = ()
    disabled: bool = False

    def __post_init__(self) -> None:
        # A lookup is a snapshot even when the caller passes a mutable collection.
        object.__setattr__(self, "unknown", frozenset(self.unknown))


def pr_phrase(pr: PrInfo) -> str:
    match pr.state:
        case PrState.OPEN:
            return f"open PR #{pr.number}"
        case PrState.MERGED:
            return f"PR #{pr.number} merged"
        case PrState.CLOSED:
            return f"closed PR #{pr.number}"
