"""The reviewed configuration that binds the sandbox Actions to one deployment.

Its own module so `action_service.catalog` can name the binding without importing the executor,
which imports the catalog's own models back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PodExecutorBinding(BaseModel):
    """Boxes started and exec'd by this service rather than reached over MCP.

    A code-owned executor exists because the caller's identity is the whole point of this Action:
    `ExecutionRequest.caller` is set by the authenticated admission path and read here directly,
    where an MCP backend would need that assertion forwarded to it over the wire.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["pod"] = "pod"
    description: str = Field(min_length=1, max_length=2000, description="Agent-visible executor description.")
    namespace: str = Field(min_length=1, description="Namespace boxes run and are exec'd in.")
    templates: dict[str, str] = Field(
        min_length=1,
        description="The boxes a caller may create: each names a PodTemplate in the namespace and says what a "
        "box made from it holds, for the agent choosing one. Only these: a caller free to name any would "
        "start whatever Pod the namespace holds a template for.",
    )
    max_timeout_seconds: int = Field(default=1800, gt=0, le=3600)
    max_output_bytes: int = Field(default=200_000, ge=0, le=1_000_000)
