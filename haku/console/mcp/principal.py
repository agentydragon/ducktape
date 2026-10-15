"""Trusted Agent identity projected for in-process MCP execution."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from haku.console.tool_call_actor import AgentActor

type AccessProfileId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]


class RequestPrincipal(BaseModel):
    """The Agent and access profile an authenticated execution acts as.

    The projection drops the Operator and credential-binding identities used for accountability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    access_profile_id: AccessProfileId | None

    @classmethod
    def from_source(cls, source: AgentActor) -> RequestPrincipal:
        """Project the bearer-authenticated Agent identity into the execution vocabulary."""

        return cls(agent_id=source.agent_id, access_profile_id=source.access_profile_id)
