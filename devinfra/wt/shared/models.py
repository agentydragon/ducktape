"""Data models and domain objects for worktree management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wt.shared.constants import MAIN_WORKTREE_DISPLAY_NAME


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
class ProcessInfo:
    """Process information for worktree usage checking."""

    pid: int
    name: str

    def __str__(self) -> str:
        return f"PID {self.pid} ({self.name})"


@dataclass
class SyncStatus:
    """Git sync status (ahead/behind counts)."""

    ahead: int
    behind: int

    @property
    def is_synced(self) -> bool:
        return self.ahead == 0 and self.behind == 0


@dataclass
class WorkingStatus:
    """Working directory status."""

    dirty_files: list[Path]
    untracked_files: list[Path]

    @property
    def is_clean(self) -> bool:
        return not self.dirty_files and not self.untracked_files

    @property
    def change_count(self) -> int:
        return len(self.dirty_files) + len(self.untracked_files)


@dataclass
class CommitInfo:
    """Commit information with proper datetime handling."""

    last_commit: str
    last_commit_message: str
    last_commit_author: str
    last_commit_date: datetime

    def format_date(self) -> str:
        return self.last_commit_date.strftime("%Y-%m-%d %H:%M")

    @property
    def short_hash(self) -> str:
        return self.last_commit[:8]
