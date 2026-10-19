"""What the Sandbox gives its runner to launch Sessions with: its identity, its once-per-Sandbox
bootstrap, and the deployment-wide platform instructions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SandboxIdentity(BaseModel):
    """The Sandbox as the Notification Service addresses it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    uid: str = Field(min_length=1)


class SandboxLaunchFile(BaseModel):
    """The file the Sandbox Controller mounts for its Sandbox (agentplane/sandbox_controller/launch.go)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sandbox: SandboxIdentity
    bootstrap: str | None = Field(default=None, min_length=1, description="Shell source the Sandbox runs once.")
