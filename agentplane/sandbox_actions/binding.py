"""The reviewed configuration that binds the sandbox Actions to one deployment.

Its own module so `action_service.catalog` can name the binding without importing the executor,
which imports the catalog's own models back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Where an offered SandboxTemplate says what a box made from it holds, for the agent choosing one:
# the `description` annotation the cluster's manifests carry on any resource that needs explaining.
DESCRIPTION_ANNOTATION = "description"


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
    templates: set[str] = Field(
        min_length=1,
        description="The SandboxTemplates in the namespace a caller may create a box from, by name. Only "
        "these: a caller free to name any would stamp whatever template the namespace holds.",
    )
    default_template: str = Field(min_length=1, description="Which of them a caller that names none gets.")
    max_timeout_seconds: int = Field(default=1800, gt=0, le=3600)
    max_output_bytes: int = Field(default=200_000, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _default_offered(self) -> SandboxExecutorBinding:
        if self.default_template not in self.templates:
            raise ValueError("default_template must name one of templates")
        return self
