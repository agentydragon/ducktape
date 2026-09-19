"""Agent-facing arguments and results for the sandbox Actions.

Shapes follow <../../../haku/console/tools/sandbox.py>, the exec surface already in daily use,
because an agent that knows one should not have to learn the other. What differs is ownership:
these sandboxes run as the ServiceAccount that called the Action, so nothing here names an
identity -- the caller's is the only one available and the executor reads it off the request.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.exec.models import ExecStream, ExitStatus

SandboxName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=48,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="DNS-label name for the sandbox, unique among this caller's. Reuse it to reach "
        "the same box again; choose another for an independent one.",
    ),
]


class SandboxState(StrEnum):
    """Where a sandbox is between asking for it and being able to run something in it."""

    PROVISIONING = "provisioning"
    READY = "ready"
    UNHEALTHY = "unhealthy"


class SandboxInfo(BaseModel):
    """Compact, non-secret state of one sandbox this surface created."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    state: SandboxState
    environment: str = Field(description="The reviewed environment name this box was created from.")
    created_at: datetime | None = None
    pod_name: str | None = Field(default=None, description="Absent until the sandbox has a Pod.")
    reason: str | None = Field(default=None, description="Why it is not ready, when it is not.")


class ExecResult(BaseModel):
    """Bounded result of one script run in a sandbox."""

    model_config = ConfigDict(extra="forbid")

    exit: ExitStatus
    stdout: ExecStream
    stderr: ExecStream
    duration_seconds: float = Field(ge=0)


class SandboxList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandboxes: list[SandboxInfo]


class DisposeResult(BaseModel):
    """Result of an idempotent disposal; disposing an absent sandbox is not an error."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    existed: bool


class ProvisionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    environment: str | None = Field(
        default=None,
        description="Which reviewed environment to create the box from; omit for this deployment's "
        "default. Only the environments this deployment configured can be named.",
    )


class ExecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    script: str = Field(
        min_length=1,
        description="Bash script text run with `bash -lc`. Pipes, redirects, globs, variable "
        "expansion and compound commands use normal Bash semantics.",
    )
    timeout_seconds: int = Field(gt=0, le=3600, description="Wall-clock ceiling for the script.")
    max_output_bytes: int = Field(
        ge=0, le=1_000_000, description="Maximum bytes retained independently from stdout and stderr."
    )
    cwd: str | None = Field(default=None, description="Working directory; omit for the environment's default.")


class NameArgs(BaseModel):
    """The whole argument of an Action that names one sandbox and nothing else."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
