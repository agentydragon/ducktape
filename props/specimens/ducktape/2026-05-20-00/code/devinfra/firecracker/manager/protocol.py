"""process_api WebSocket protocol types (from RE of the binary).

These model the messages exchanged over the WebSocket connection to
process_api (Anthropic's PID 1 binary in Firecracker VMs). See
devinfra/claude/web_env/re/process_api/ for the full reverse-engineered
source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Client → Server ─────────────────────────────────────────────────────────


class CreateProcess(BaseModel):
    """Spawn a new process."""

    name: str = Field(description="Command to execute; also used as process ID")
    args: list[str] = Field(default_factory=list)
    env_vars: dict[str, str] | None = None
    clear_env: bool = False
    uid: int = 0
    gid: int = 0
    reattachable: bool = False
    allow_process_id_reuse: bool = True
    timeout: int | None = Field(default=None, description="Kill after N seconds")
    memory_limit_bytes: int | None = None


class ProcessConnection(BaseModel):
    """Reattach to an existing process."""

    process_id: str
    reattach: bool = True
    expected_container_name: str | None = None
    want_trace_events: bool = False


# ── Server → Client (JSON dicts with a single key) ──────────────────────────


class ServerMessageKey(StrEnum):
    """Top-level keys in server→client JSON dict messages."""

    PROCESS_CREATED = "ProcessCreated"
    PROCESS_CREATED_V2 = "ProcessCreatedV2"
    PROCESS_EXITED = "ProcessExited"
    PROCESS_TIMED_OUT = "ProcessTimedOut"
    PROCESS_OUT_OF_MEMORY = "ProcessOutOfMemory"
    CONTAINER_OUT_OF_MEMORY = "ContainerOutOfMemory"
    INFRA_ERROR = "InfraError"
    CONNECTION_CAPABILITIES = "ConnectionCapabilities"
    ATTACHED_TO_PROCESS = "AttachedToProcess"
    ATTACHED_TO_PROCESS_V2 = "AttachedToProcessV2"


class ProcessExited(BaseModel):
    status: int
    details: str


class ProcessTimedOut(BaseModel):
    timeout_secs: int
    details: str


# ── Server → Client (bare JSON strings) ─────────────────────────────────────


class ServerTag(StrEnum):
    """Server→client messages sent as bare JSON strings (not dicts)."""

    EXPECT_STDOUT = "ExpectStdOut"
    EXPECT_STDERR = "ExpectStdErr"
    STDOUT_EOF = "StdOutEOF"
    STDERR_EOF = "StdErrEOF"
    KEEPALIVE = "KeepAlive"
    CLOSED = "Closed"


NOOP_TAGS = frozenset({ServerTag.STDOUT_EOF, ServerTag.STDERR_EOF, ServerTag.KEEPALIVE, ServerTag.CLOSED})


# ── Stream events (used by the client to yield typed output) ─────────────────


@dataclass
class Stdout:
    data: bytes


@dataclass
class Stderr:
    data: bytes


@dataclass
class Exited:
    status: int
    details: str


@dataclass
class TimedOut:
    timeout_secs: int
    details: str


@dataclass
class OOMKilled:
    pass


@dataclass
class InfraError:
    message: str


ProcessEvent = Stdout | Stderr | Exited | TimedOut | OOMKilled | InfraError
