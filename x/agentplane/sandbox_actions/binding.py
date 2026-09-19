"""The reviewed configuration that binds the sandbox Actions to one deployment.

Its own module so `action_service.catalog` can name the binding without importing the executor,
which imports the catalog's own models back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SandboxEnvironment(BaseModel):
    """One box shape a caller may ask for, by name.

    The template is named here and never by the caller: a free-form template argument would let
    whoever may call this Action stamp any template in the namespace, the integration app's runner
    template included.
    """

    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, description="SandboxTemplate in the sandbox namespace.")
    container: str = Field(min_length=1, description="Container in that template's Pod that commands run in.")
    default_cwd: str = Field(min_length=1, description="Working directory an exec uses when it names none.")
    description: str = Field(
        min_length=1, max_length=2000, description="What this box holds, for the agent picking it."
    )


class SandboxExecutorBinding(BaseModel):
    """Sandboxes stamped and exec'd by this service rather than reached over MCP.

    A code-owned executor exists because the caller's identity is the whole point of this Action:
    `ExecutionRequest.caller` is set by the authenticated admission path and read here directly,
    where an MCP backend would need that assertion forwarded to it over the wire.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["sandbox"] = "sandbox"
    description: str = Field(min_length=1, max_length=2000, description="Agent-visible executor description.")
    namespace: str = Field(min_length=1, description="Namespace Sandboxes are stamped into and exec'd in.")
    environments: dict[str, SandboxEnvironment] = Field(
        min_length=1, description="The box shapes this deployment offers, keyed by the name a caller may ask for."
    )
    default_environment: str = Field(min_length=1, description="Which of them a caller that names none gets.")
    max_timeout_seconds: int = Field(default=1800, gt=0, le=3600)
    max_output_bytes: int = Field(default=200_000, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _default_exists(self) -> SandboxExecutorBinding:
        if self.default_environment not in self.environments:
            raise ValueError("default_environment must name one of environments")
        return self
