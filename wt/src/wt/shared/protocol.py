"""JSON-RPC 2.0 protocol for wt daemon communication.

Uses standard JSON-RPC 2.0 for type-safe, standardized RPC communication
between clients and the wt multiplexing daemon.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
import json
from pathlib import Path
from typing import Annotated, Literal, NewType, cast
import uuid

from pydantic import BaseModel, Field, ValidationError

from .github_models import PRData

# WorktreeID: Deliberately scrambled identifier to prevent accidental misuse
WorktreeID = NewType("WorktreeID", str)  # Opaque to clients; server owns parsing


# WorktreeID helpers (make/parse) are server-only (see wt.server.worktree_ids)


class DaemonHealthStatus(StrEnum):
    """Daemon health status levels."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class DaemonHealth(BaseModel):
    """Daemon health information."""

    status: DaemonHealthStatus
    last_error: str | None = None
    last_error_time: datetime | None = None
    github_errors: int = 0
    gitstatusd_errors: int = 0


class Request(BaseModel):
    """JSON-RPC 2.0 request."""

    model_config = {"extra": "forbid"}

    jsonrpc: str = "2.0"
    method: str = Field(..., description="Method name to call")
    params: dict[str, object] = Field(default_factory=dict, description="Method parameters")
    id: uuid.UUID = Field(..., description="Request ID")


class Response(BaseModel):
    model_config = {"extra": "forbid"}

    jsonrpc: str = "2.0"
    result: (
        StatusResponse
        | PingResult
        | WorktreeCreateResult
        | WorktreeDeleteResult
        | WorktreeListResult
        | WorktreeIdentifyResult
        | WorktreeGetByNameResult
        | WorktreeResolvePathResult
        | TeleportResult
        | str
    ) = Field(..., description="Result data")
    id: uuid.UUID = Field(..., description="Request ID from original request")


class Error(BaseModel):
    """JSON-RPC 2.0 error object."""

    model_config = {"extra": "forbid"}

    code: int = Field(..., description="Error code")
    message: str = Field(..., description="Error message")
    data: object | None = Field(default=None, description="Additional error data")


class ErrorResponse(BaseModel):
    """JSON-RPC 2.0 error response."""

    model_config = {"extra": "forbid"}

    jsonrpc: str = "2.0"
    error: Error = Field(..., description="Error details")
    id: uuid.UUID = Field(..., description="Request ID from original request")


# Standard JSON-RPC error codes
class ErrorCodes(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # Custom error codes (application-specific)
    WORKTREE_NOT_FOUND = -32001
    GITSTATUSD_ERROR = -32002


# Method parameter schemas
class StatusParams(BaseModel):
    """Parameters for status requests."""

    model_config = {"extra": "forbid"}

    worktree_ids: list[WorktreeID] = Field(
        default_factory=list, description="List of worktree IDs. If empty, returns all discovered worktrees."
    )


class WorktreeCreateParams(BaseModel):
    """Parameters for worktree creation."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., description="Simple worktree name (no slashes)")
    source_wtid: WorktreeID | None = Field(
        default=None, description="Server-minted WorktreeID of source worktree to copy from (determines source commit)"
    )
    source_branch: str | None = Field(
        default=None, description="Optional base branch to create from (overrides upstream when provided)"
    )


class WorktreeDeleteParams(BaseModel):
    """Parameters for worktree deletion."""

    model_config = {"extra": "forbid"}

    wtid: WorktreeID = Field(..., description="Worktree identifier to delete")
    force: bool = Field(default=False, description="Force deletion")


class WorktreeIdentifyParams(BaseModel):
    """Parameters for worktree identification."""

    model_config = {"extra": "forbid"}

    absolute_path: Path = Field(..., description="Absolute filesystem path")


class WorktreeGetByNameParams(BaseModel):
    """Parameters for worktree lookup by name."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., description="Worktree name to look up")


class PRRefreshParams(BaseModel):
    """Parameters to synchronously refresh PR cache for a worktree."""

    model_config = {"extra": "forbid"}

    wtid: WorktreeID = Field(..., description="Worktree identifier")


class WorktreeResolvePathParams(BaseModel):
    """Parameters for path resolution within worktrees."""

    model_config = {"extra": "forbid"}

    worktree_name: str | None = Field(default=None, description="Target worktree name, or None for current")
    path_spec: str = Field(..., description="Path to resolve (/, ./, or unprefixed)")
    current_path: Path = Field(..., description="Current working directory for relative resolution")


class WorktreeTeleportTargetParams(BaseModel):
    """Parameters for computing cd target with path preservation."""

    model_config = {"extra": "forbid"}

    target_name: str = Field(..., description="Target worktree name")
    current_path: Path = Field(..., description="Current working directory")


# Method result schemas
class CommitInfo(BaseModel):
    """Git commit information."""

    hash: str = Field(..., description="Full commit hash")
    short_hash: str = Field(..., description="Short commit hash")
    message: str = Field(..., description="Commit message")
    author: str = Field(..., description="Commit author")
    date: str = Field(..., description="Commit date in ISO format")


# Algebraic (tagged) PR info for wire
class PRInfoOk(BaseModel):
    type: Literal["ok"] = "ok"
    pr_data: PRData


class PRInfoError(BaseModel):
    type: Literal["error"] = "error"
    error: str


class PRInfoDisabled(BaseModel):
    type: Literal["disabled"] = "disabled"


PRInfo = Annotated[PRInfoOk | PRInfoError | PRInfoDisabled, Field(discriminator="type")]


class StatusResult(BaseModel):
    """Result for individual worktree status."""

    wtid: WorktreeID = Field(..., description="Worktree identifier")
    name: str = Field(..., description="Human-readable name")
    absolute_path: Path = Field(..., description="Absolute filesystem path")
    branch_name: str = Field(..., description="Git branch name")
    dirty_files_lower_bound: int = Field(..., description="Lower bound on modified file count reported by gitstatusd")
    untracked_files_lower_bound: int = Field(
        ..., description="Lower bound on untracked file count reported by gitstatusd"
    )
    processing_time_ms: float = Field(..., description="Processing time in milliseconds")
    last_updated_at: datetime = Field(..., description="When gitstatusd was last queried for this worktree")
    is_cached: bool = Field(default=False, description="Whether this result came from cache")
    cache_age_ms: float | None = Field(
        default=None, description="Age of cached data in milliseconds (None if not cached)"
    )
    is_stale: bool = Field(default=False, description="Whether cached data is considered stale by server policy")
    commit_info: CommitInfo | None = Field(default=None, description="Latest commit information if available")
    ahead_count: int = Field(default=0, description="Number of commits ahead of upstream branch")
    behind_count: int = Field(default=0, description="Number of commits behind upstream branch")
    is_main: bool = Field(default=False, description="Whether this is the main repository")
    upstream_branch: str = Field(..., description="Upstream branch name for ahead/behind calculations")
    pr_info: PRInfo = Field(
        default_factory=PRInfoDisabled, description="GitHub pull request information (ok | error | disabled)"
    )
    gitstatusd_state: GitstatusdState | None = Field(default=None, description="gitstatusd runtime state")
    restarts: int = Field(default=0, description="Number of restarts observed")
    last_error: str | None = Field(default=None, description="Last error message if any")

    @property
    def has_dirty_files(self) -> bool:
        """True if gitstatusd reported any staged or unstaged changes."""
        return self.dirty_files_lower_bound > 0

    @property
    def has_untracked_files(self) -> bool:
        """True if gitstatusd reported any untracked files."""
        return self.untracked_files_lower_bound > 0


class StatusItem(BaseModel):
    status: StatusResult
    processing_time_ms: float


class StatusResponse(BaseModel):
    """Unified response for get_status method (always returns results for multiple worktrees)."""

    items: dict[WorktreeID, StatusItem] = Field(
        ..., description="Map of worktree ID to item {status, processing_time_ms}"
    )
    total_processing_time_ms: float = Field(..., description="Total processing time in milliseconds")
    discovery_time_ms: float = Field(default=0.0, description="Time spent on worktree discovery")
    concurrent_requests: int = Field(default=1, description="Number of requests processed concurrently")
    daemon_health: DaemonHealth = Field(..., description="Current daemon health status and error information")
    readiness_summary: ReadinessSummary | None = Field(default=None, description="Overall readiness summary (optional)")
    components: ComponentsStatus | None = Field(
        default=None, description="Top-level component states (discovery/github/gitstatusd)"
    )


class PingResult(BaseModel):
    """Result for ping method."""

    message: str = "pong"
    daemon_pid: int = Field(..., description="Process ID of the daemon")
    started_at: datetime = Field(..., description="When the daemon was started")
    discovered_worktrees: list[Path] = Field(default_factory=list, description="List of discovered worktree paths")


class StartupPhase(StrEnum):
    STARTING = "starting"
    DISCOVERING = "discovering"
    WARMING_UP = "warming_up"
    READY = "ready"


class StartupMessage(BaseModel):
    success: bool
    protocol_version: int = 1
    pid: int
    timestamp: float
    ready: bool = False
    phase: StartupPhase | None = None
    discovered_worktrees: list[str] = Field(default_factory=list)
    gitstatusd_path: str | None = None
    socket_path: str | None = None
    error: str | None = None


class WorktreeInfo(BaseModel):
    """Information about a worktree."""

    wtid: WorktreeID = Field(..., description="Worktree identifier")
    name: str = Field(..., description="Human-readable name")
    absolute_path: Path = Field(..., description="Absolute filesystem path")
    branch_name: str = Field(..., description="Git branch name")
    exists: bool = Field(..., description="Whether directory exists")
    is_main: bool = Field(..., description="Whether this is main repo")


class HookRunResult(BaseModel):
    ran: bool = Field(..., description="Whether the hook attempted to run")
    exit_code: int | None = Field(default=None, description="Exit code if process returned")
    stdout: str | None = Field(default=None, description="Captured stdout")
    stderr: str | None = Field(default=None, description="Captured stderr")
    error: str | None = Field(
        default=None, description="High-level error: not_found/not_file/timeout/exception or exception message"
    )
    timeout_secs: float | None = Field(default=None, description="Timeout value used when error=='timeout'")
    streamed: bool | None = Field(
        default=None, description="True if output was streamed live; stdout/stderr may be truncated previews"
    )


class WorktreeCreateResult(BaseModel):
    """Result for worktree creation."""

    wtid: WorktreeID = Field(..., description="Created worktree ID")
    name: str = Field(..., description="Human-readable name")
    absolute_path: Path = Field(..., description="Absolute filesystem path")
    branch_name: str = Field(..., description="Git branch name")
    success: bool = Field(..., description="Operation success")
    post_hook: HookRunResult | None = Field(default=None, description="Post-creation hook run result (if configured)")


class WorktreeDeleteResult(BaseModel):
    """Result for worktree deletion."""

    wtid: WorktreeID = Field(..., description="Deleted worktree ID")
    success: bool = Field(..., description="Deletion success")
    message: str = Field(..., description="Operation message")


class WorktreeListResult(BaseModel):
    """Result for worktree listing."""

    worktrees: list[WorktreeInfo] = Field(..., description="List of worktrees")


class WorktreeIdentifyResult(BaseModel):
    """Result for worktree identification."""

    wtid: WorktreeID | None = Field(..., description="Worktree ID if identified")
    name: str | None = Field(..., description="Human-readable name if identified")
    is_worktree: bool = Field(..., description="Whether path is a managed worktree")
    relative_path: str | None = Field(..., description="Relative path within worktree if identified")


class WorktreeGetByNameResult(BaseModel):
    """Result for worktree lookup by name."""

    wtid: WorktreeID | None = Field(..., description="Worktree ID if found")
    name: str | None = Field(..., description="Human-readable name if found")
    exists: bool = Field(..., description="Whether worktree exists")
    absolute_path: Path | None = Field(..., description="Absolute path if found")


class WorktreeResolvePathResult(BaseModel):
    """Result for path resolution within worktrees."""

    absolute_path: Path = Field(..., description="Resolved absolute filesystem path")


class TeleportCdThere(BaseModel):
    type: Literal["cd_there"] = "cd_there"
    cd_path: Path


class TeleportDoesNotExist(BaseModel):
    type: Literal["does_not_exist"] = "does_not_exist"
    name: str = Field(..., description="Requested worktree name that was not found on server")


TeleportResult = Annotated[TeleportCdThere | TeleportDoesNotExist, Field(discriminator="type")]


class StreamEventType(StrEnum):
    PROGRESS = "progress"
    HOOK_OUTPUT = "hook_output"


class ProgressOperation(StrEnum):
    WORKTREE_CREATE = "worktree_create"


class WorktreeCreateStep(StrEnum):
    CHECKOUT_STARTED = "checkout_started"
    CHECKOUT_DONE = "checkout_done"
    HYDRATE_STARTED = "hydrate_started"
    HYDRATE_DONE = "hydrate_done"


class HookStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class ProgressEvent(BaseModel):
    """Progress update for streaming operations (discriminated by event='progress')."""

    event: Literal["progress"] = "progress"
    operation: ProgressOperation = Field(..., description="Operation name")
    step: WorktreeCreateStep = Field(..., description="Current step")
    progress: float = Field(..., description="Progress 0.0-1.0")
    message: str = Field(..., description="Progress message")


class HookOutputEvent(BaseModel):
    """Streaming output from post-creation hook (discriminated by event='hook_output')."""

    event: Literal["hook_output"] = "hook_output"
    stream: HookStream
    data: str


# Discriminated union for stream messages from server
StreamMessage = Annotated[ProgressEvent | HookOutputEvent, Field(discriminator="event")]


class ComponentState(StrEnum):
    OK = "ok"
    SCANNING = "scanning"
    STARTING = "starting"
    ERROR = "error"
    DISABLED = "disabled"


class GitstatusdState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


class ComponentStatus(BaseModel):
    state: ComponentState
    last_error: str | None = None
    metrics: dict[str, int | float] = Field(default_factory=dict)


class ComponentsStatus(BaseModel):
    discovery: ComponentStatus
    github: ComponentStatus
    gitstatusd: ComponentStatus


class ReadinessSummary(BaseModel):
    total_worktrees: int = Field(..., description="Total discovered worktrees")
    with_gitstatusd: int = Field(..., description="Worktrees with running gitstatusd")
    discovery_scanning: bool = Field(default=False, description="Discovery currently scanning")
    github: ComponentState = Field(default=ComponentState.DISABLED, description="GitHub component state")


def create_error_response(code: int, message: str, request_id: uuid.UUID, data: object | None = None) -> ErrorResponse:
    """Create a JSON-RPC 2.0 error response."""
    error = Error(code=code, message=message, data=data)
    return ErrorResponse(error=error, id=request_id)


def parse_request(data: str) -> Request:
    """Parse JSON string into JSON-RPC request."""
    try:
        raw_data = json.loads(data)
        return cast(Request, Request.model_validate(raw_data))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON (parse error): {e}") from e
    except ValidationError as e:
        raise ValueError(f"Invalid JSON-RPC request schema: {e}") from e


class NoParams(BaseModel):
    """Explicit empty params schema for methods without parameters."""

    model_config = {"extra": "forbid"}


# Method registry for type safety
