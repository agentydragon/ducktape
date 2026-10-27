"""Deploy-time Agent access-profile catalog."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccessProfile(BaseModel):
    """Configured identity profile ID retained on an Agent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")


class ConsoleConfigFile(BaseModel):
    """Non-secret catalog of Agent access-profile identities."""

    model_config = ConfigDict(extra="forbid")

    access_profiles: list[AccessProfile] = Field(min_length=1)
    default_access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

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
