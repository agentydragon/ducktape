"""Deploy-time Agent access-profile catalog."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class AccessProfile(BaseModel):
    """Configured identity profile ID retained on an Agent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_profile_fields(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            value.pop("auto_approval_policy", None)
            value.pop("in_process_server_ids", None)
        return value


class ConsoleConfigFile(BaseModel):
    """Non-secret access-profile catalog, with compatibility for retired deployment fields."""

    model_config = ConfigDict(extra="forbid")

    access_profiles: list[AccessProfile] = Field(min_length=1)
    default_access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_deployment_fields(cls, value: object) -> object:
        # Remove retired deployment fields before Settings applies `extra="forbid"`. They can
        # remain projected briefly while the new image rolls out, but have no runtime effect.
        if isinstance(value, dict):
            value = dict(value)
            value.pop("auto_approval_policies", None)
            value.pop("mcp", None)
            # Older ConfigMaps can overlap with this image during rollout. Static Agents are no
            # longer reconciled, so ignore their retired catalog while that ConfigMap is replaced.
            if "static_agents" in value:
                logger.warning("ignoring retired static_agents Console configuration")
                value.pop("static_agents")
        return value

    @model_validator(mode="after")
    def _require_unique_identity(self) -> ConsoleConfigFile:
        profiles: dict[str, AccessProfile] = {}
        for profile in self.access_profiles:
            if profile.id in profiles:
                raise ValueError(f"duplicate access profile id {profile.id!r}")
            profiles[profile.id] = profile
        if self.default_access_profile_id not in profiles:
            raise ValueError(f"default access profile {self.default_access_profile_id!r} is not configured")

        return self
