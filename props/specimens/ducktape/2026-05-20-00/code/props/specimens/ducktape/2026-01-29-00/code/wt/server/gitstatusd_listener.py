"""GitStatusd protocol implementation with proper type checking and validation.

GitStatusd communicates via stdin/stdout with ASCII separators:
- Record separator: ASCII 30 (0x1E)
- Unit separator: ASCII 31 (0x1F)

See: https://github.com/romkatv/gitstatus for full protocol specification.

Reactive architecture:
- GitstatusdListener owns its own Signal[Collector[GitstatusdData]]
- Updates its signal directly when status changes
- No callbacks needed - reaktiv dependency tracking handles propagation
"""

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from reaktiv import Signal

from wt.shared.protocol import Collector, GitstatusdData

logger = logging.getLogger(__name__)


class GitStatusdError(Exception):
    """Base exception for gitstatusd protocol errors."""


class GitStatusdParseError(GitStatusdError):
    """Error parsing gitstatusd response."""


class GitStatusdValidationError(GitStatusdError):
    """Error validating gitstatusd response fields."""


class RepositoryState(StrEnum):
    """Repository state/action as reported by gitstatusd."""

    NORMAL = ""
    MERGE = "merge"
    REBASE = "rebase"
    REBASE_INTERACTIVE = "rebase-i"
    REBASE_MERGE = "rebase-m"
    APPLY_MAILBOX = "am"
    APPLY_MAILBOX_OR_REBASE = "am/rebase"
    CHERRY_PICK = "cherry-pick"
    REVERT = "revert"
    BISECT = "bisect"


@dataclass(frozen=True)
class GitStatusdRequest:
    """GitStatusd request format."""

    request_id: str
    directory_path: str
    disable_index_computation: bool = False

    def to_wire_format(self) -> str:
        """Convert to gitstatusd wire format."""
        disable_flag = "1" if self.disable_index_computation else "0"
        return f"{self.request_id}\x1f{self.directory_path}\x1f{disable_flag}\x1e"


@dataclass(frozen=True)
class GitStatusdResponse:
    """GitStatusd response with all fields properly typed and validated."""

    # Core fields
    request_id: str
    is_git_repository: bool

    # Repository information (only present if is_git_repository=True)
    git_workdir: str | None = None
    commit_hash: str | None = None
    local_branch: str | None = None
    upstream_branch: str | None = None
    remote_name: str | None = None
    remote_url: str | None = None
    repository_state: RepositoryState | None = None

    # File counts
    index_file_count: int | None = None
    staged_changes: int | None = None
    unstaged_changes: int | None = None
    conflicted_changes: int | None = None
    untracked_files: int | None = None

    # Branch tracking
    commits_ahead_upstream: int | None = None
    commits_behind_upstream: int | None = None
    stash_count: int | None = None

    # Additional metadata
    last_tag: str | None = None
    unstaged_deleted_files: int | None = None
    staged_new_files: int | None = None
    staged_deleted_files: int | None = None

    # Push remote information
    push_remote_name: str | None = None
    push_remote_url: str | None = None
    commits_ahead_push_remote: int | None = None
    commits_behind_push_remote: int | None = None

    # Index flags
    skip_worktree_files: int | None = None
    assume_unchanged_files: int | None = None

    # Commit message
    commit_message_encoding: str | None = None
    commit_message_summary: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.is_git_repository and (self.staged_changes or self.unstaged_changes or self.untracked_files))

    @property
    def has_dirty_files(self) -> bool:
        return bool(self.is_git_repository and (self.staged_changes or self.unstaged_changes))

    @property
    def has_untracked_files(self) -> bool:
        return bool(self.is_git_repository and self.untracked_files)

    @property
    def is_ahead_of_upstream(self) -> bool:
        """True if local branch is ahead of upstream."""
        return bool(self.commits_ahead_upstream)

    @property
    def is_behind_upstream(self) -> bool:
        """True if local branch is behind upstream."""
        return bool(self.commits_behind_upstream)


@dataclass(frozen=True)
class GitstatusdCountLimits:
    """Configured count limits used when spawning gitstatusd."""

    staged: int
    unstaged: int
    conflicted: int
    untracked: int

    def limit_hit(self, value: int | None, kind: Literal["staged", "unstaged", "conflicted", "untracked"]) -> bool:
        limits = {
            "staged": self.staged,
            "unstaged": self.unstaged,
            "conflicted": self.conflicted,
            "untracked": self.untracked,
        }
        limit = limits[kind]
        return limit >= 0 and value is not None and value >= limit


@dataclass(frozen=True)
class GitstatusWorkingSummary:
    """Snapshot of staged/unstaged/untracked counts surfaced to callers."""

    staged_changes: int | None
    unstaged_changes: int | None
    conflicted_changes: int | None
    untracked_files: int | None
    staged_limit_hit: bool
    unstaged_limit_hit: bool
    untracked_limit_hit: bool
    last_updated_at: datetime | None
    has_cache: bool
    last_error: str | None

    @property
    def dirty_lower_bound(self) -> int | None:
        if self.staged_changes is None or self.unstaged_changes is None:
            return None
        return self.staged_changes + self.unstaged_changes

    @property
    def dirty_limit_hit(self) -> bool:
        return self.staged_limit_hit or self.unstaged_limit_hit

    @property
    def untracked_lower_bound(self) -> int | None:
        return self.untracked_files

    @classmethod
    def empty(cls, *, last_error: str | None = None) -> Self:
        return cls(
            staged_changes=None,
            unstaged_changes=None,
            conflicted_changes=None,
            untracked_files=None,
            staged_limit_hit=False,
            unstaged_limit_hit=False,
            untracked_limit_hit=False,
            last_updated_at=None,
            has_cache=False,
            last_error=last_error,
        )


SHA_HEX_LEN = 40


class GitStatusdProtocol:
    """GitStatusd protocol handler with proper type checking and validation."""

    # Expected minimum number of fields for a valid git repository response
    # Matches gitstatusd v1.5+ protocol; bump if upstream adds fields
    MIN_GIT_REPO_FIELDS = 29

    @staticmethod
    def parse_response(raw_response: str) -> GitStatusdResponse:
        """Parse gitstatusd response with comprehensive validation.

        Args:
            raw_response: Raw response string from gitstatusd

        Returns:
            Parsed and validated GitStatusdResponse

        Raises:
            GitStatusdParseError: If response format is invalid
            GitStatusdValidationError: If response fields are invalid
        """
        try:
            # Remove record separator and split on unit separator
            response_data = raw_response.rstrip("\x1e")
            if not response_data:
                raise GitStatusdParseError("Empty response from gitstatusd")

            fields = response_data.split("\x1f")

            # Validate minimum field count
            if len(fields) < 2:
                raise GitStatusdParseError(f"Invalid response: expected at least 2 fields, got {len(fields)}")

            # Parse core fields
            request_id = fields[0]

            try:
                is_git_repository = int(fields[1]) == 1
            except (ValueError, IndexError) as e:
                raise GitStatusdValidationError(f"Invalid git repository flag: {e}") from e

            # If not a git repository, return minimal response
            if not is_git_repository:
                logger.debug("Directory is not a git repository (request_id=%s)", request_id)
                return GitStatusdResponse(request_id=request_id, is_git_repository=False)

            # For git repositories, validate we have enough fields
            if len(fields) < GitStatusdProtocol.MIN_GIT_REPO_FIELDS:
                raise GitStatusdParseError(
                    f"Incomplete git repository response: expected {GitStatusdProtocol.MIN_GIT_REPO_FIELDS} fields, "
                    f"got {len(fields)}"
                )

            # Invariant: after this point, `fields` has at least MIN_GIT_REPO_FIELDS entries.
            # Helper accessors (_safe_get_*) therefore may assume the indexed positions exist;
            # an IndexError here would indicate a protocol mismatch upstream and should fail loudly.
            assert len(fields) >= GitStatusdProtocol.MIN_GIT_REPO_FIELDS

            # Parse git repository fields with proper validation
            return GitStatusdResponse(
                request_id=request_id,
                is_git_repository=True,
                git_workdir=GitStatusdProtocol._safe_get_optional_string(fields, 2),
                commit_hash=GitStatusdProtocol._safe_get_commit_hash(fields, 3),
                local_branch=GitStatusdProtocol._safe_get_optional_string(fields, 4),
                upstream_branch=GitStatusdProtocol._safe_get_optional_string(fields, 5),
                remote_name=GitStatusdProtocol._safe_get_optional_string(fields, 6),
                remote_url=GitStatusdProtocol._safe_get_optional_string(fields, 7),
                repository_state=GitStatusdProtocol._safe_get_repository_state(fields, 8),
                index_file_count=GitStatusdProtocol._safe_get_int(fields, 9),
                staged_changes=GitStatusdProtocol._safe_get_int(fields, 10),
                unstaged_changes=GitStatusdProtocol._safe_get_int(fields, 11),
                conflicted_changes=GitStatusdProtocol._safe_get_int(fields, 12),
                untracked_files=GitStatusdProtocol._safe_get_int(fields, 13),
                commits_ahead_upstream=GitStatusdProtocol._safe_get_int(fields, 14),
                commits_behind_upstream=GitStatusdProtocol._safe_get_int(fields, 15),
                stash_count=GitStatusdProtocol._safe_get_int(fields, 16),
                last_tag=GitStatusdProtocol._safe_get_optional_string(fields, 17),
                unstaged_deleted_files=GitStatusdProtocol._safe_get_int(fields, 18),
                staged_new_files=GitStatusdProtocol._safe_get_int(fields, 19),
                staged_deleted_files=GitStatusdProtocol._safe_get_int(fields, 20),
                push_remote_name=GitStatusdProtocol._safe_get_optional_string(fields, 21),
                push_remote_url=GitStatusdProtocol._safe_get_optional_string(fields, 22),
                commits_ahead_push_remote=GitStatusdProtocol._safe_get_int(fields, 23),
                commits_behind_push_remote=GitStatusdProtocol._safe_get_int(fields, 24),
                skip_worktree_files=GitStatusdProtocol._safe_get_int(fields, 25),
                assume_unchanged_files=GitStatusdProtocol._safe_get_int(fields, 26),
                commit_message_encoding=GitStatusdProtocol._safe_get_optional_string(fields, 27),
                commit_message_summary=GitStatusdProtocol._safe_get_optional_string(fields, 28),
            )

        except (GitStatusdParseError, GitStatusdValidationError):
            raise
        except Exception as e:
            raise GitStatusdParseError(f"Unexpected error parsing gitstatusd response: {e}") from e

    @staticmethod
    def _safe_get_string(fields: list[str], index: int) -> str:
        """Get required string field with validation."""
        try:
            value = fields[index]
            if not value:
                raise GitStatusdValidationError(f"Required field {index} is empty")
            return value
        except IndexError:
            raise GitStatusdValidationError(f"Missing required field {index}") from None

    @staticmethod
    def _safe_get_optional_string(fields: list[str], index: int) -> str | None:
        """Get optional string field, returning None for empty strings."""
        value = fields[index]
        return value if value else None

    @staticmethod
    def _safe_get_int(fields: list[str], index: int) -> int | None:
        """Get integer field with validation."""
        value = fields[index]
        if not value:
            return None
        try:
            return int(value)
        except ValueError as e:
            raise GitStatusdValidationError(f"Invalid integer in field {index}: {e}") from e

    @staticmethod
    def _safe_get_commit_hash(fields: list[str], index: int) -> str | None:
        """Get commit hash with validation."""
        value = fields[index]
        if not value:
            return None

        # Validate commit hash format (40 hex characters)
        if len(value) != SHA_HEX_LEN or not all(c in "0123456789abcdef" for c in value.lower()):
            raise GitStatusdValidationError(f"Invalid commit hash format: {value}")

        return value

    @staticmethod
    def _safe_get_repository_state(fields: list[str], index: int) -> RepositoryState:
        """Get repository state enum with validation (do not mask errors)."""
        try:
            value = fields[index]
        except IndexError as e:
            raise GitStatusdValidationError("Missing repository state field") from e

        if value == "":
            # Empty string denotes NORMAL repository state per protocol
            return RepositoryState.NORMAL

        # Find matching repository state
        for state in RepositoryState:
            if state.value == value:
                return state

        # Unknown state: treat as validation error
        raise GitStatusdValidationError(f"Unknown repository state: {value}")


def find_gitstatusd_in_runfiles() -> str | None:
    """Check Bazel runfiles for a hermetically-provided gitstatusd binary."""
    runfiles_dir = os.environ.get("TEST_SRCDIR") or os.environ.get("RUNFILES_DIR")
    if not runfiles_dir:
        return None
    candidate = Path(runfiles_dir) / "_main" / "third_party" / "gitstatusd" / "gitstatusd-linux-x86_64"
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def find_gitstatusd(config) -> tuple[str | None, str | None]:
    """Find and validate gitstatusd binary using config, runfiles, or PATH.

    Returns (path, error_message). On success, error_message is None.
    Checks config first, then Bazel runfiles, then PATH.
    """
    if config.gitstatusd_path:
        gitstatusd_path = str(config.gitstatusd_path)
        try:
            result = subprocess.run([gitstatusd_path, "--version"], check=False, capture_output=True, timeout=2)
            if result.returncode == 0:
                return gitstatusd_path, None
            return None, (f"Configured gitstatusd path not working: {gitstatusd_path} (exit code {result.returncode})")
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
            return None, f"Configured gitstatusd path failed: {gitstatusd_path} ({e})"

    runfiles_path = find_gitstatusd_in_runfiles()
    if runfiles_path:
        return runfiles_path, None

    gitstatusd_cmd = "gitstatusd"
    if shutil.which(gitstatusd_cmd):
        try:
            result = subprocess.run([gitstatusd_cmd, "--version"], check=False, capture_output=True, timeout=2)
            if result.returncode == 0:
                return gitstatusd_cmd, None
            return None, (f"gitstatusd found on PATH but not working (exit code {result.returncode})")
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
            return None, f"gitstatusd found on PATH but failed to execute: {e}"

    return None, (
        "gitstatusd binary not found. Please install gitstatusd and ensure it's available on PATH, "
        "or configure gitstatusd_path in your config file. "
        "Common installation: brew install romkatv/gitstatus/gitstatus"
    )


class GitstatusdListener:
    """Manages a gitstatusd process for a single worktree.

    Owns a Signal[Collector[GitstatusdData]] that it updates when status changes.
    Register this signal with the store to include in aggregation.
    """

    def __init__(self, path: Path, config, git_manager):
        self.path = path
        self.config = config
        self.git_manager = git_manager
        self.process: asyncio.subprocess.Process | None = None
        self._count_limits = GitstatusdCountLimits(staged=-1, unstaged=-1, conflicted=-1, untracked=-1)
        self._status_updating: bool = False

        # Own our reactive state - register with store
        self.status: Signal[Collector[GitstatusdData]] = Signal(Collector())

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        gitstatusd_path, err = find_gitstatusd(self.config)
        if not gitstatusd_path:
            self.status.update(lambda c: c.error(err or "gitstatusd_missing"))
            return
        self.process = await asyncio.create_subprocess_exec(
            gitstatusd_path,
            "--num-threads=8",
            "--max-num-staged=-1",
            "--max-num-unstaged=-1",
            "--max-num-conflicted=-1",
            "--max-num-untracked=-1",
            "--max-commit-summary-length=0",
            "--repo-ttl-seconds=3600",
            "--log-level=FATAL",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        if not self.process:
            return
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=5.0)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.process = None

    @property
    def is_running(self) -> bool:
        return bool(self.process and self.process.returncode is None)

    async def update_working_status(self) -> None:
        if not self.process or self.process.returncode is not None:
            await self.start()
        if not self.process or not self.process.stdin or not self.process.stdout:
            self.status.update(lambda c: c.error("gitstatusd process not ready"))
            return
        if self._status_updating:
            return
        self._status_updating = True
        try:
            request_id = str(uuid.uuid4())[:8]
            gitstatusd_request = GitStatusdRequest(
                request_id=request_id, directory_path=str(self.path), disable_index_computation=False
            )
            request_data = gitstatusd_request.to_wire_format()
            if not (self.process and self.process.stdin and self.process.stdout):
                raise RuntimeError("gitstatusd process is not ready")
            self.process.stdin.write(request_data.encode())
            await self.process.stdin.drain()
            response = await self.process.stdout.readuntil(b"\x1e")
            response_str = response.decode("utf-8")
            parsed_response = GitStatusdProtocol.parse_response(response_str)

            # Convert to GitstatusdData and update signal
            data = self._build_gitstatusd_data(parsed_response)
            self.status.update(lambda c: c.ok(data))
        except Exception:
            logger.exception("gitstatusd update failed for %s", self.path)
            self.status.update(lambda c: c.error("gitstatusd update failed"))
        finally:
            self._status_updating = False

    def _build_gitstatusd_data(self, response: GitStatusdResponse) -> GitstatusdData:
        """Convert gitstatusd response to unified GitstatusdData."""
        if not response.is_git_repository:
            return GitstatusdData(
                branch=None,
                commit=None,
                staged=0,
                unstaged=0,
                untracked=0,
                conflicted=0,
                remote_ahead=None,
                remote_behind=None,
            )

        return GitstatusdData(
            branch=response.local_branch,
            commit=response.commit_hash,
            staged=response.staged_changes or 0,
            unstaged=response.unstaged_changes or 0,
            untracked=response.untracked_files or 0,
            conflicted=response.conflicted_changes or 0,
            remote_ahead=response.commits_ahead_upstream,
            remote_behind=response.commits_behind_upstream,
        )
