"""Data models and domain objects for worktree management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from x.wt.shared.constants import MAIN_WORKTREE_DISPLAY_NAME


@dataclass
class Worktree:
    """Unified worktree representation that eliminates path resolution duplication."""

    name: str
    path: Path
    branch: str
    is_main: bool = False

    @classmethod
    def main_repo(cls, repo_path: Path, branch: str) -> Worktree:
        return cls(name=MAIN_WORKTREE_DISPLAY_NAME, path=repo_path, branch=branch, is_main=True)

    def exists(self) -> bool:
        return self.path.exists()


@dataclass
class SyncStatus:
    """Git sync status (ahead/behind counts)."""

    ahead: int
    behind: int


@dataclass
class CommitInfo:
    """Commit information with proper datetime handling."""

    last_commit: str
    last_commit_message: str
    last_commit_author: str
    last_commit_date: datetime

    @property
    def short_hash(self) -> str:
        return self.last_commit[:8]
