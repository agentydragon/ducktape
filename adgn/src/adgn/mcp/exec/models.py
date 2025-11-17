from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from signal import SIGKILL, SIGTERM
import time
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# Signal exit codes for process termination
SIGNAL_EXIT_OFFSET: Final[int] = 128


def signal_exit_code(sig: int) -> int:
    return SIGNAL_EXIT_OFFSET + int(sig)


def perf_timer() -> float:
    """Get current performance counter time."""
    return time.perf_counter()


def duration_ms_from_start(start_time: float) -> int:
    """Calculate duration in milliseconds from start time."""
    return round((time.perf_counter() - start_time) * 1000)


def duration_ms_from_loop_start(start_time: float, loop: asyncio.AbstractEventLoop) -> int:
    """Calculate duration in milliseconds from asyncio loop start time."""
    return round((loop.time() - start_time) * 1000)


@asynccontextmanager
async def async_timer() -> AsyncGenerator[Callable[[], int], None]:
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
MAX_BYTES_CAP = 100_000

# Cap for execution timeout across exec-like servers (milliseconds)
# Keep reasonably low to avoid runaway processes; tune per product needs.
MAX_EXEC_TIMEOUT_MS = 300_000

# Pydantic-validated types
TimeoutMs = Annotated[int, Field(gt=0, le=MAX_EXEC_TIMEOUT_MS)]
MaxBytes = Annotated[int, Field(ge=0, le=MAX_BYTES_CAP)]


class TruncatedStream(BaseModel):
    """Truncated stream output with metadata about the original size.

    Only used when output exceeds byte limits. Complete output uses plain str instead.
    """

    truncated_text: str
    total_bytes: int

    model_config = ConfigDict(extra="forbid")


# Union type for exec output that may or may not be truncated
ExecStream = str | TruncatedStream


class ExecOutput(BaseModel):
    """Raw stdout/stderr produced by an executed process."""

    stdout: bytes
    stderr: bytes

    model_config = ConfigDict(extra="forbid")


class TimedOut(BaseModel):
    """Process was terminated after exceeding the timeout."""

    kind: Literal["timed_out"] = "timed_out"

    model_config = ConfigDict(extra="forbid")


class Exited(BaseModel):
    """Process exited normally with an exit code."""

    kind: Literal["exited"] = "exited"
    exit_code: int

    model_config = ConfigDict(extra="forbid")


class Killed(BaseModel):
    """Process was killed by a signal."""

    kind: Literal["killed"] = "killed"
    signal: int

    model_config = ConfigDict(extra="forbid")


ExitStatus = Annotated[TimedOut | Exited | Killed, Field(discriminator="kind")]


class ExecOutcome(BaseModel):
    """Complete execution outcome with output and exit status."""

    output: ExecOutput
    exit: ExitStatus
    duration_ms: int = Field(description="Execution duration in milliseconds")

    model_config = ConfigDict(extra="forbid")


class ExecInput(BaseModel):
    """Typed payload for container exec tool.

    Prefer passing cmd as a list to avoid shell quoting issues. Set shell=True to run via
    'sh -lc <cmd>' when providing a single string command assembled server-side.

    Note: TTY is not allocated for processes to ensure stdout/stderr separation.
    """

    cmd: list[str] = Field(description="Command to run; pass list to avoid shell quoting issues")
    cwd: Path | None = Field(default=None, description="Working directory inside container")
    env: dict[str, str] | None = Field(default=None, description="Environment variables for the process")
    user: str | None = Field(default=None, description="Username inside container")
    shell: bool = Field(default=False, description="Run via sh -lc <cmd>")
    timeout_ms: TimeoutMs = Field(description="Timeout in milliseconds; sends TERM (exit_code becomes None)")

    model_config = ConfigDict(extra="forbid")


class BaseExecResult(BaseModel):
    """Standard MCP exec response - basic servers return this directly.

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


def clamp_stdin_bytes(stdin_text: str | None, limit: int) -> bytes:
    """Encode stdin_text to UTF-8 and clamp to at most limit bytes.

    Returns bytes to write to the child's stdin (no markers added).
    """
    if not stdin_text:
        return b""
    data = stdin_text.encode("utf-8", errors="replace")
    if limit <= 0:
        return b""
    if len(data) <= limit:
        return data
    return data[:limit]


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


def render_outcome_to_result(outcome: ExecOutcome, max_bytes: int) -> BaseExecResult:
    """Render ExecOutcome directly to BaseExecResult (preserves types)."""
    stdout_render = render_stream(outcome.output.stdout, max_bytes)
    stderr_render = render_stream(outcome.output.stderr, max_bytes)

    # Pass through exit status directly - all subprocess types are valid MCP types
    exit_status = outcome.exit

    return BaseExecResult.from_rendered_streams(exit_status, stdout_render, stderr_render, outcome.duration_ms)


def render_raw_to_result(
    stdout: bytes, stderr: bytes, exit_code: int | None, timed_out: bool, max_bytes: int, duration_ms: int
) -> BaseExecResult:
    """Render raw streams directly to BaseExecResult (preserves types)."""
    stdout_render = render_stream(stdout, max_bytes)
    stderr_render = render_stream(stderr, max_bytes)

    # exit_code should be int when not timed out, but handle None defensively
    exit_status = TimedOut() if timed_out else Exited(exit_code=exit_code if exit_code is not None else 0)

    return BaseExecResult.from_rendered_streams(exit_status, stdout_render, stderr_render, duration_ms)


def read_stream_limited_sync(fh, store_limit: int, chunk_size: int = 8192) -> StreamReadResult:
    """Read a blocking binary stream to EOF, storing at most store_limit bytes.

    - Always drains to EOF to compute total_bytes
    - Returns stored_text (UTF-8), truncated flag, and total_bytes
    """
    assert store_limit >= 0
    stored = bytearray()
    total = 0
    while True:
        buf = fh.read(chunk_size)
        if not buf:
            break
        total += len(buf)
        # Store only up to the cap
        if len(stored) < store_limit:
            # How many bytes from this chunk can we still store?
            remaining = store_limit - len(stored)
            if remaining > 0:
                stored.extend(buf[:remaining])
        # else: we still need to drain to count total
    truncated = total > store_limit
    return StreamReadResult(stored_bytes=bytes(stored), truncated=truncated, total_bytes=total)


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
