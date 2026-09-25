"""Agent-facing arguments and results for the sandbox Actions.

Shapes follow <../../haku/console/tools/sandbox.py>, the exec surface already in daily use,
because an agent that knows one should not have to learn the other. What differs is ownership:
these sandboxes run as the ServiceAccount that called the Action, so nothing here names an
identity -- the caller's is the only one available and the executor reads it off the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

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


# The condition the controller publishes for "this box can run something", and what `exec`
# requires. Named here because the executor gates on it and the Action descriptions tell an agent
# to poll it; those two must be the same predicate.
READY_CONDITION = "Ready"


class SandboxCondition(BaseModel):
    """One status condition, as the Agent Sandbox controller wrote it.

    Passed through rather than summarised: the controller owns this lifecycle, and a reading taken
    here would be a second opinion that can disagree with the authority and carries less than it
    did. `reason` is the machine-readable half an agent can branch on (`DependenciesReady`,
    `WarmPoolNotFound`); `message` is the sentence a human wants.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    type: str
    status: str = Field(description='"True", "False" or "Unknown", as Kubernetes spells a condition.')
    reason: str | None = None
    message: str | None = None
    last_transition_time: datetime | None = None


class SandboxInfo(BaseModel):
    """Non-secret state of one sandbox this surface created, as the API server reports it."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    template: str = Field(description="The SandboxTemplate this box was created from.")
    conditions: list[SandboxCondition] = Field(
        description=f"The controller's own conditions, verbatim. {READY_CONDITION!r} with "
        'status "True" is what `exec` requires; until then that condition says why, and a '
        '"Suspended" condition means a box that is stopped rather than still coming up.'
    )
    created_at: datetime | None = None
    pod_name: str | None = Field(
        default=None, description="Absent until the sandbox has a Pod, and again if that Pod goes away."
    )
    node_name: str | None = Field(default=None, description="Where the controller placed the Pod.")
    pod_ips: list[str] = Field(default_factory=list)


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


class CreateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SandboxName
    template: str | None = Field(
        default=None,
        description="Which SandboxTemplate to create the box from; omit for this deployment's default. "
        "Only the templates this deployment offers can be named.",
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
    cwd: str | None = Field(
        default=None, description="Working directory; omit to start in the container's own working directory."
    )


class NameArgs(BaseModel):
    """The whole argument of an Action that names one sandbox and nothing else."""

    model_config = ConfigDict(extra="forbid")

    name: SandboxName


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
