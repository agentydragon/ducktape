"""Deploy-config environment passthrough for harness CLI processes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from util.env import EnvironmentVariableName


class EnvironmentPassthrough(BaseModel):
    """Base for implementation configs whose deploy entry may set extra CLI environment."""

    model_config = ConfigDict(frozen=True)

    environment: dict[EnvironmentVariableName, str] = Field(
        default_factory=dict,
        description=(
            "Extra environment for the harness CLI process, merged last over the computed proxy "
            "and provider variables — an explicit deploy-config value wins. E.g. "
            "ENABLE_TOOL_SEARCH asserts to the Claude CLI that the gateway passes the "
            "tool-search betas through, which it never probes for (#5155)."
        ),
    )
