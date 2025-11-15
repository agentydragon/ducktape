"""Unified Git operations manager combining all git functionality."""

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import re
import subprocess

import pygit2

from ..shared.configuration import Configuration
from ..shared.git_utils import git_run
from ..shared.protocol import CommitInfo

logger = logging.getLogger(__name__)


# Exception classes for GitManager
class GitError(Exception):
    pass


class GitTimeoutError(GitError):
    pass


class NoSuchRefError(GitError):
    pass


class NoSuchBranchError(GitError):
    pass


class WorktreeError(GitError):
    pass


class WorktreeDeleteError(WorktreeError):
    pass


class WorktreeCreateError(WorktreeError):
    pass


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    exists: bool
    is_main: bool


@dataclass
class GitManager:
    config: Configuration

    def __post_init__(self) -> None:
        self._main_repo: pygit2.Repository = pygit2.Repository(str(self.config.main_repo))

    def branch_exists(self, branch_name: str) -> bool:
        return branch_name in self._main_repo.branches

    def create_branch(self, branch_name: str, source_branch: str = "HEAD") -> None:
        if not self.branch_exists(branch_name):
            target_obj = self._main_repo.revparse_single(source_branch)
            target_commit = target_obj.peel(pygit2.Commit)
            self._main_repo.branches.local.create(branch_name, target_commit)

    async def get_working_directory_status(self) -> tuple[list[Path], list[Path]]:
        """Get working directory status using fastest available method. Returns absolute Path objects."""
        try:
            # Get status - dirty (staged/modified) and untracked files
            dirty_files = []
            untracked_files = []

            for file_path, flags in self._main_repo.status().items():
                if flags & (pygit2.GIT_STATUS_WT_MODIFIED | pygit2.GIT_STATUS_INDEX_MODIFIED):
                    dirty_files.append(Path(self.config.main_repo) / file_path)
                elif flags & pygit2.GIT_STATUS_WT_NEW:
                    untracked_files.append(Path(self.config.main_repo) / file_path)

            return dirty_files, untracked_files

        except (pygit2.GitError, OSError) as e:
            raise GitError("Failed to get working directory status") from e

    def get_repo(self, path: Path | None = None) -> pygit2.Repository:
        if path is None or path == self.config.main_repo:
            return self._main_repo
        return pygit2.Repository(str(path))

    def get_commit_info(self, ref: str, worktree: Path) -> CommitInfo:
        repo = self.get_repo(worktree)
        try:
            # Resolve reference to commit object in the given repo
            resolved = repo.resolve_refish(ref)  # type: ignore[attr-defined]
            commit = resolved[0]
        except KeyError as e:
            raise NoSuchRefError(f"Cannot get commit object for {ref}: {e}") from e

        message = commit.message
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")

        return CommitInfo(
            hash=str(commit.id),
            short_hash=str(commit.id)[:8],
            message=message.strip(),
            author=commit.author.name,
            date=datetime.fromtimestamp(commit.commit_time, UTC).isoformat(),
        )

    def verify_ref_exists(self, ref: str) -> str:
        try:
            resolved = self._main_repo.resolve_refish(ref)  # type: ignore[attr-defined]
            return str(resolved[0].id)
        except KeyError as e:
            raise NoSuchRefError(f"Reference does not exist: {ref}") from e

    # Worktree operations
    def list_worktrees(self) -> list[WorktreeInfo]:
        """List all worktrees using pygit2 API."""
        current_branch = self._main_repo.head.shorthand if not self._main_repo.head_is_detached else None

        worktree_infos = [
            WorktreeInfo(path=self.config.main_repo, branch=current_branch or "", exists=True, is_main=True)
        ]

        # Add all other worktrees; compute branch name only when repo is valid
        for wt_name in self._main_repo.list_worktrees():
            wt_path = Path(self._main_repo.lookup_worktree(wt_name).path)
            exists = wt_path.exists()
            branch_name = ""
            if exists:
                try:
                    wt_repo = pygit2.Repository(str(wt_path))
                    if not wt_repo.head_is_detached:
                        branch_name = wt_repo.head.shorthand or ""
                except (pygit2.GitError, OSError, ValueError, TypeError):
                    # Treat as non-existent/invalid repo; leave branch_name empty
                    exists = False
            worktree_infos.append(WorktreeInfo(path=wt_path, branch=branch_name, exists=exists, is_main=False))

        return worktree_infos

    def worktree_add(self, path: Path, branch: str) -> None:
        path_obj = path

        # Validate path doesn't already exist
        if path_obj.exists():
            raise WorktreeCreateError(f"Path {path} already exists")

        # Validate branch name format (basic check)
        if not branch or not branch.strip():
            raise WorktreeCreateError("Branch name cannot be empty")

        # Check if branch name contains valid characters only
        if not re.match(r"^[a-zA-Z0-9._/-]+$", branch):
            raise WorktreeCreateError(f"Branch name '{branch}' contains invalid characters")

        # Check if worktree already exists for this path
        if any(info.path == path_obj for info in self.list_worktrees()):
            raise WorktreeCreateError(f"Worktree already exists at {path}")

        # Ensure branch exists; if not, create it off upstream
        if self._main_repo.lookup_branch(branch) is None:
            target_obj = self._main_repo.revparse_single(self.config.upstream_branch)
            target = target_obj.peel(pygit2.Commit)
            self._main_repo.branches.local.create(branch, target)

        # Use git CLI to create worktree without checkout (critical for large repos)
        # Rationale: pygit2 lacks a no-checkout worktree-add equivalent with matching performance
        # for very large repos; consolidating via CLI here avoids heavy libgit operations.
        try:
            git_run(["worktree", "add", "--no-checkout", path_obj, branch], cwd=self.config.main_repo)
        except subprocess.CalledProcessError as e:
            raise WorktreeCreateError(f"git worktree add failed: {e.stderr.decode(errors='replace').strip()}") from e

    def worktree_remove(self, path: Path, force: bool = False) -> None:
        path_obj = path
        args: list[str | os.PathLike[str]] = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(path_obj)
        try:
            git_run(args, cwd=self.config.main_repo)
        except subprocess.CalledProcessError as e:
            raise WorktreeDeleteError(
                f"Failed to remove worktree at {path}: {e.stderr.decode(errors='replace').strip()}"
            ) from e

    def verify_branch_exists(self, branch: str) -> str:
        try:
            resolved = self._main_repo.resolve_refish(f"refs/heads/{branch}")  # type: ignore[attr-defined]
            return str(resolved[0].id)
        except KeyError as e:
            raise NoSuchBranchError(f"Branch {branch} does not exist") from e
