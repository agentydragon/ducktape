from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from signal import SIGKILL, SIGTERM
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Signal exit codes for process termination
SIGNAL_EXIT_OFFSET: Final[int] = 128


def signal_exit_code(sig: int) -> int:
    return SIGNAL_EXIT_OFFSET + int(sig)


@asynccontextmanager
async def async_timer() -> AsyncGenerator[Callable[[], int]]:
    """Async context manager for timing operations.

    Usage:
        async with async_timer() as get_duration_ms:
            # do work
            duration_ms = get_duration_ms()
    """
    loop = asyncio.get_running_loop()
    start_time = loop.time()

    def get_duration_ms() -> int:
        return round((loop.time() - start_time) * 1000)

    yield get_duration_ms


EXIT_CODE_SIGTERM: Final[int] = signal_exit_code(SIGTERM)
EXIT_CODE_SIGKILL: Final[int] = signal_exit_code(SIGKILL)

# Cap for stdout/stderr/stdin bytes in exec-like servers
MAX_BYTES_CAP = 150_000

# Cap for execution timeout across exec-like servers (milliseconds)
# Keep reasonably low to avoid runaway processes; tune per product needs.
MAX_EXEC_TIMEOUT_MS = 300_000

# Pydantic-validated types
TimeoutMs = Annotated[int, Field(gt=0, le=MAX_EXEC_TIMEOUT_MS)]


class TruncatedStream(BaseModel):
    """Truncated stream output with metadata about the original size.

    Only used when output exceeds byte limits. Complete output uses plain str instead.
    """

    truncated_text: str
    total_bytes: int


# Union type for exec output that may or may not be truncated
ExecStream = str | TruncatedStream


@dataclass(frozen=True)
class ExecOutput:
    """Raw stdout/stderr produced by an executed process (internal model)."""

    stdout: bytes
    stderr: bytes


class TimedOut(BaseModel):
    """Process was terminated after exceeding the timeout."""

    kind: Literal["timed_out"] = "timed_out"


class Exited(BaseModel):
    """Process exited normally with an exit code."""

    kind: Literal["exited"] = "exited"
    exit_code: int


class Killed(BaseModel):
    """Process was killed by a signal."""

    kind: Literal["killed"] = "killed"
    signal: int


ExitStatus = Annotated[TimedOut | Exited | Killed, Field(discriminator="kind")]


@dataclass(frozen=True)
class ExecOutcome:
    """Complete execution outcome with output and exit status (internal model)."""

    output: ExecOutput
    exit: ExitStatus
    duration_ms: int


# Corresponds to Docker Engine API v1.52 ContainerExec:
# https://docs.docker.com/reference/api/engine/version/v1.52/#tag/Exec/operation/ContainerExec
# Specifically: Cmd (command array) and Env (environment variables as "NAME=value" strings)

# Type alias for environment variable validation
EnvVar = Annotated[
    str,
    Field(
        description="Environment variable in 'NAME=value' format",
        pattern=r"^[^=]+=.*$",  # Must have at least one char before '=', anything after
    ),
]


class ExecInput(OpenAIStrictModeBaseModel):
    """Typed payload for container exec tool.

    Note: TTY is not allocated for processes to ensure stdout/stderr separation.
    """

    cmd: list[str] = Field(
        description="Command array passed directly to Docker exec API (no shell). "
        "DO NOT include shell quotes around arguments - array elements are passed as-is. "
        "WRONG: ['sed', '-n', \"'1,10p'\", 'file'] (quotes in string). "
        "RIGHT: ['sed', '-n', '1,10p', 'file'] (no quotes). "
        "For shell features (pipes, globs), use: ['sh', '-c', 'sed -n 1,10p file | head']"
    )
    # str not Path: OpenAI strict mode doesn't accept format="path" in JSON schemas
    cwd: str | None = Field(description="Working directory inside container (None = container default)")
    env: list[EnvVar] | None = Field(
        description="Environment variables as ['NAME=value', ...] (None = inherit container env)"
    )
    user: str | None = Field(description="Username inside container (None = container default)")
    timeout_ms: TimeoutMs = Field(description="Timeout in milliseconds; sends TERM (exit status becomes TimedOut)")

    def env_dict(self) -> dict[str, str]:
        """Convert env list to dict for internal use."""
        if not self.env:
            return {}
        result = {}
        for env_str in self.env:
            name, value = env_str.split("=", 1)
            result[name] = value
        return result


def make_exec_input(
    cmd: list[str],
    *,
    timeout_ms: int = 10_000,
    cwd: str | None = None,
    env: list[str] | None = None,
    user: str | None = None,
) -> ExecInput:
    """Convenience helper for constructing ExecInput with sensible defaults.

    Mirrors the pattern from tests/conftest.py:make_exec_input() and
    bootstrap.docker_exec_call() but for production code. Use this to avoid
    repeating the full 5-field constructor when most fields are None.

    Example:
        exec_input = make_exec_input(["echo", "hello"])
        exec_input_with_timeout = make_exec_input(["sleep", "5"], timeout_ms=6_000)
    """
    return ExecInput(cmd=cmd, cwd=cwd, env=env, user=user, timeout_ms=timeout_ms)


class BaseExecResult(BaseModel):
    """Standard MCP exec response - basic servers return this directly (output model).

    Preserves str | TruncatedStream distinction to encode truncation information.
    """

    exit: ExitStatus
    stdout: ExecStream
    stderr: ExecStream
    duration_ms: int = Field(description="Execution duration in milliseconds")
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_rendered_streams(
        cls, exit_status: ExitStatus, stdout: ExecStream, stderr: ExecStream, duration_ms: int, **extras
    ) -> BaseExecResult:
        """Create from already-rendered streams (no None handling needed)."""
        return cls(exit=exit_status, stdout=stdout, stderr=stderr, duration_ms=duration_ms, **extras)


# Stream processing functions (moved from io_limits.py to avoid circular imports)


@dataclass(slots=True)
class StreamReadResult:
    stored_bytes: bytes  # Prefix captured (up to limit)
    truncated: bool  # True if output exceeded the store_limit
    total_bytes: int  # total bytes produced by the stream (counted)

    @property
    def stored_text(self) -> str:
        return _decode_prefix(self.stored_bytes)


def _decode_prefix(prefix: bytes) -> str:
    """Decode a byte prefix to UTF-8, replacing errors; avoid surrogate noise."""
    return prefix.decode("utf-8", errors="replace")


def render_stream(data: bytes, limit: int) -> ExecStream:
    """Render raw bytes under a byte limit, producing either text or TruncatedStream."""
    if limit <= 0 or len(data) == 0:
        return ""
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace")
    return TruncatedStream(truncated_text=data[:limit].decode("utf-8", errors="replace"), total_bytes=len(data))


def render_streams(stdout: bytes, stderr: bytes, max_bytes: int) -> tuple[ExecStream, ExecStream]:
    """Render both stdout and stderr streams under a byte limit."""
    return (render_stream(stdout, max_bytes), render_stream(stderr, max_bytes))


def render_outcome_to_result(outcome: ExecOutcome, max_bytes: int) -> BaseExecResult:
    """Render ExecOutcome directly to BaseExecResult (preserves types)."""
    stdout_render, stderr_render = render_streams(outcome.output.stdout, outcome.output.stderr, max_bytes)
    return BaseExecResult.from_rendered_streams(outcome.exit, stdout_render, stderr_render, outcome.duration_ms)


def render_raw_to_result(
    stdout: bytes, stderr: bytes, exit_code: int | None, timed_out: bool, max_bytes: int, duration_ms: int
) -> BaseExecResult:
    """Render raw streams directly to BaseExecResult (preserves types)."""
    stdout_render, stderr_render = render_streams(stdout, stderr, max_bytes)

    # exit_code should be int when not timed out, but handle None defensively
    exit_status = TimedOut() if timed_out else Exited(exit_code=exit_code if exit_code is not None else 0)

    return BaseExecResult.from_rendered_streams(exit_status, stdout_render, stderr_render, duration_ms)


async def read_stream_limited_async(
    reader: asyncio.StreamReader, store_limit: int, chunk_size: int = 8192
) -> StreamReadResult:
    """Read an asyncio StreamReader to EOF, storing at most store_limit bytes.

    - Always drains to EOF to compute total_bytes
    - Returns stored_text (UTF-8), truncated flag, and total_bytes
    """
    assert store_limit >= 0
    stored = bytearray()
    total = 0
    while True:
        buf = await reader.read(chunk_size)
        if not buf:
            break
        total += len(buf)
        if len(stored) < store_limit:
            remaining = store_limit - len(stored)
            if remaining > 0:
                stored.extend(buf[:remaining])
    truncated = total > store_limit
    return StreamReadResult(stored_bytes=bytes(stored), truncated=truncated, total_bytes=total)
