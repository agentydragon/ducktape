"""Deploy-time Agent identity catalog."""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.identity.naming import normalize_agent_name


class AccessProfile(BaseModel):
    """Configured identity profile ID retained on an Agent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_tool_call_fields(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            value.pop("auto_approval_policy", None)
            value.pop("in_process_server_ids", None)
        return value


class StaticAgentEntry(BaseModel):
    """Controller-owned identity and credentials for one static Agent slot."""

    agent_id: UUID
    display_name: str
    token: SecretStr
    operator_subject: str = Field(min_length=1)
    access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

    @field_validator("display_name")
    @classmethod
    def _require_normalized_display_name(cls, value: str) -> str:
        normalized = normalize_agent_name(value)
        if normalized.display_name != value:
            raise ValueError(f"static Agent display_name must be normalized as {normalized.display_name!r}")
        return value


class LoadedStaticAgent(BaseModel):
    """A static Agent after reading its credential, before canonical Operator resolution."""

    agent_id: UUID
    display_name: str
    secret_reference: str
    token: SecretStr
    operator_external_user_key: str
    access_profile_id: str


class ConsoleConfigFile(BaseModel):
    """Non-secret Agent catalog, with narrow compatibility for retired tool-call fields."""

    model_config = ConfigDict(extra="forbid")

    access_profiles: list[AccessProfile] = Field(min_length=1)
    default_access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    static_agents: dict[str, StaticAgentEntry] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_tool_call_fields(cls, value: object) -> object:
        # Remove retired MCP catalog fields before Settings applies `extra="forbid"`. They can
        # remain projected briefly while the new image rolls out, but have no runtime effect.
        if isinstance(value, dict):
            value = dict(value)
            value.pop("auto_approval_policies", None)
            value.pop("mcp", None)
        return value

    @model_validator(mode="after")
    def _require_unique_identity(self) -> ConsoleConfigFile:
        agent_ids: set[UUID] = set()
        name_keys: set[str] = set()
        for slot, agent in self.static_agents.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
                raise ValueError(f"invalid static Agent slot {slot!r}")
            name_key = normalize_agent_name(agent.display_name).reservation_key
            if agent.agent_id in agent_ids:
                raise ValueError(f"duplicate static Agent id {agent.agent_id}")
            if name_key in name_keys:
                raise ValueError(f"duplicate normalized static Agent display name {agent.display_name!r}")
            agent_ids.add(agent.agent_id)
            name_keys.add(name_key)

        profiles: dict[str, AccessProfile] = {}
        for profile in self.access_profiles:
            if profile.id in profiles:
                raise ValueError(f"duplicate access profile id {profile.id!r}")
            profiles[profile.id] = profile
        if self.default_access_profile_id not in profiles:
            raise ValueError(f"default access profile {self.default_access_profile_id!r} is not configured")

        for agent in self.static_agents.values():
            if agent.access_profile_id not in profiles:
                raise ValueError(
                    f"static Agent {agent.agent_id} references unknown access profile {agent.access_profile_id!r}"
                )
        return self


def load_static_agents(config: ConsoleConfigFile) -> list[LoadedStaticAgent]:
    """Validate static-agent credentials once before canonical Operator resolution."""
    loaded: list[LoadedStaticAgent] = []
    seen_tokens: set[str] = set()
    for slot, entry in config.static_agents.items():
        token = entry.token.get_secret_value()
        if token in seen_tokens:
            raise RuntimeError("duplicate static agent bearer tokens")
        seen_tokens.add(token)
        loaded.append(
            LoadedStaticAgent(
                agent_id=entry.agent_id,
                display_name=entry.display_name,
                secret_reference=slot,
                token=entry.token,
                operator_external_user_key=entry.operator_subject,
                access_profile_id=entry.access_profile_id,
            )
        )
    return loaded
