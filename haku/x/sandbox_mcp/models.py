"""Agent-facing models for the Haku sandbox MCP server."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.exec.models import ExecStream, ExitStatus

SandboxName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description=(
            "Stable DNS-label name for the sandbox claim. Reuse the same name to resume an "
            "existing matching sandbox; choose a new name for an independent environment."
        ),
    ),
]

SandboxState = Literal["provisioning", "ready", "unhealthy", "expired"]
BootstrapState = Literal["pending", "running", "succeeded", "failed"]
SandboxWarningKind = Literal[
    "warm_pool_changed", "container_changed", "default_cwd_changed", "bootstrap_script_changed", "provenance_unknown"
]


class SandboxWarning(BaseModel):
    """One non-fatal divergence between the running sandbox and current configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: SandboxWarningKind = Field(description="Enumerable divergence category, for scanning a page of sandboxes.")
    detail: str = Field(description="What the claim records and what the current configuration names.")


class SandboxInfo(BaseModel):
    """Compact, non-secret state for one managed sandbox claim."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: SandboxState
    healthy: bool
    created_at: datetime | None = None
    expires_at: datetime
    sandbox_name: str | None = None
    pod_name: str | None = None
    bootstrap_state: BootstrapState
    reason: str | None = None
    message: str | None = None
    warnings: list[SandboxWarning] = Field(
        default_factory=list,
        description=(
            "Ways this sandbox no longer matches the current configuration. Empty means no known "
            "divergence. The sandbox stays usable regardless; deciding whether the skew matters is "
            "the caller's."
        ),
    )


class SandboxExecResult(BaseModel):
    """Bounded result from one Bash execution in a sandbox."""

    model_config = ConfigDict(extra="forbid")

    exit: ExitStatus
    stdout: ExecStream
    stderr: ExecStream
    duration_seconds: float = Field(ge=0, description="Wall-clock command duration in seconds.")
    expires_at: datetime = Field(description="Confirmed sandbox deadline after the pre-exec TTL refresh.")


class SandboxListPage(BaseModel):
    """One Kubernetes-backed page of managed sandboxes."""

    model_config = ConfigDict(extra="forbid")

    sandboxes: list[SandboxInfo]
    continue_token: str | None = Field(
        default=None, description="Opaque token for the next page; null means this is the final page."
    )


class DisposeSandboxResult(BaseModel):
    """Result of an idempotent claim disposal request."""

    model_config = ConfigDict(extra="forbid")

    name: str
    deleted: bool = Field(description="True when a managed claim existed and deletion was requested.")
