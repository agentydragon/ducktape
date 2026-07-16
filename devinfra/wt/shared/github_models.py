from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class GitHubError(Exception):
    pass


class PRStatus(StrEnum):
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    OPEN_MERGEABLE = "OPEN_MERGEABLE"
    OPEN_CONFLICTING = "OPEN_CONFLICTING"
    OPEN_UNKNOWN = "OPEN_UNKNOWN"

    @property
    def is_merged(self) -> bool:
        return self == PRStatus.MERGED

    @property
    def is_open(self) -> bool:
        return self.name.startswith("OPEN_")

    @property
    def is_closed(self) -> bool:
        return self == PRStatus.CLOSED

    @property
    def display_text(self) -> str:
        if self == PRStatus.MERGED:
            return "merged"
        if self == PRStatus.CLOSED:
            return "closed"
        if self == PRStatus.OPEN_MERGEABLE:
            return "can merge"
        if self == PRStatus.OPEN_CONFLICTING:
            return "conflict"
        if self == PRStatus.OPEN_UNKNOWN:
            return "open"
        return self.value.lower()


class PRState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"

    @property
    def is_merged(self) -> bool:
        return self == PRState.MERGED


class PRMergeability(StrEnum):
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"


class PullRequestSearch(BaseModel):
    number: int
    title: str
    state: PRState
    url: str


class PullRequestList(BaseModel):
    number: int
    head_ref_name: str = Field(alias="headRefName")
    state: PRState
    title: str
    merged_at: str | None = Field(None, alias="mergedAt")


class PRData(BaseModel):
    pr_number: int
    pr_state: PRState
    draft: bool = False
    mergeable: bool | None = None
    merged_at: str | None = None
    additions: int | None = None
    deletions: int | None = None


class GitHubPRResponse(BaseModel):
    """Raw GitHub PR API response data"""

    number: int
    state: PRState
    title: str
    draft: bool = False
    mergeable: bool | None = None
    merged_at: str | None = None
    additions: int | None = None
    deletions: int | None = None

    @classmethod
    def from_github_pr(cls, pr) -> GitHubPRResponse:
        """Create from PyGithub PR object"""
        return cls(
            number=pr.number,
            state=pr.state,
            title=pr.title,
            draft=pr.draft,
            mergeable=pr.mergeable,
            merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
            additions=pr.additions,
            deletions=pr.deletions,
        )


class PRInfoRepr(BaseModel):
    branch: str
    pr_data: PRData | None = None
    gh_error: str | None = None


def coerce_prdata(src: Any) -> PRData:
    if isinstance(src, PRData):
        return src
    if isinstance(src, GitHubPRResponse):
        return PRData(
            pr_number=src.number,
            pr_state=PRState(src.state),
            draft=src.draft,
            mergeable=src.mergeable,
            merged_at=src.merged_at,
            additions=src.additions,
            deletions=src.deletions,
        )
    if isinstance(src, dict):
        num = src["pr_number"] if "pr_number" in src else src["number"]
        st = src.get("pr_state")
        raw_state = st if st is not None else src.get("state")
        if raw_state is None:
            raise KeyError("state")
        state = raw_state if isinstance(raw_state, PRState) else PRState(str(raw_state))
        return PRData(
            pr_number=int(num),
            pr_state=state,
            draft=bool(src.get("draft", False)),
            mergeable=src.get("mergeable"),
            merged_at=src.get("merged_at"),
            additions=src.get("additions"),
            deletions=src.get("deletions"),
        )
    raise TypeError("Unsupported PR data type")


@runtime_checkable
class HasBasicPR(Protocol):  # minimal protocol for PyGithub-like PR (read-only properties OK)
    @property
    def number(self) -> int: ...

    @property
    def state(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def draft(self) -> bool: ...

    @property
    def mergeable(self) -> bool | None: ...

    @property
    def merged_at(self) -> datetime | None: ...

    @property
    def additions(self) -> int | None: ...

    @property
    def deletions(self) -> int | None: ...


@dataclass
class PRInfo:
    branch: str
    pr_data: PRData | None = None
    github_pr: HasBasicPR | None = None  # runtime object, not serialized
    gh_error: str | None = None

    def to_repr(self) -> PRInfoRepr:
        return PRInfoRepr(branch=self.branch, pr_data=self.pr_data, gh_error=self.gh_error)
