"""Shared principal vocabulary for time-boxed Console grants.

A grant principal answers only who receives and may exercise a permission. Source
ToolCall provenance, requester identity, and lifecycle ownership remain separate.

Callers construct :class:`RequestPrincipal` only from authenticated runtime identity.
Request payloads may select a distinct :class:`GrantPrincipal` explicitly, but never the
caller's trusted identity.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from haku.console.tool_call_actor import AgentActor

type AccessProfileId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]


class GrantPrincipalKind(StrEnum):
    AGENT = "agent"
    ACCESS_PROFILE = "access_profile"


class AgentGrantPrincipal(BaseModel):
    """Every authenticated execution of one Agent receives the grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantPrincipalKind.AGENT] = GrantPrincipalKind.AGENT
    agent_id: UUID


class AccessProfileGrantPrincipal(BaseModel):
    """Every authenticated Agent assigned to one configured access profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantPrincipalKind.ACCESS_PROFILE] = GrantPrincipalKind.ACCESS_PROFILE
    access_profile_id: AccessProfileId


type GrantPrincipal = Annotated[AgentGrantPrincipal | AccessProfileGrantPrincipal, Field(discriminator="kind")]
type GrantPrincipalInput = Literal["self"] | GrantPrincipal

# Configuration has no authenticated-session lifecycle to bind, so it may name only principals
# that remain meaningful across restarts and deployments.
type ConfigGrantPrincipal = Annotated[AgentGrantPrincipal | AccessProfileGrantPrincipal, Field(discriminator="kind")]


class RequestPrincipal(BaseModel):
    """Complete trusted authenticated identity attempting to exercise a grant or SAR.

    The access profile is also a grant-principal dimension. An Agent may request a grant for
    any valid principal; the manually approved ToolCall decides whether that request creates a
    grant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    access_profile_id: AccessProfileId | None

    @classmethod
    def from_source(cls, source: AgentActor) -> RequestPrincipal:
        """Project the bearer-authenticated Agent identity into the request-principal vocabulary,
        dropping the operator and credential-binding identity that applicability must not read."""

        return cls(agent_id=source.agent_id, access_profile_id=source.access_profile_id)


def resolve_grant_principal_input(
    requested_principal: GrantPrincipalInput, request_principal: RequestPrincipal
) -> GrantPrincipal:
    """Resolve the ``self`` shorthand or preserve an explicitly requested principal.

    Explicit principals may name any valid Agent or access profile. The manually approved
    ToolCall is the Operator's decision point for whether that request creates a grant.
    """

    if requested_principal == "self":
        return AgentGrantPrincipal(agent_id=request_principal.agent_id)
    return requested_principal


def grant_principal_from_columns(
    kind: GrantPrincipalKind, *, agent_id: UUID | None, access_profile_id: AccessProfileId | None
) -> GrantPrincipal:
    """Reconstruct a grant principal from the relational principal columns."""

    match kind:
        case GrantPrincipalKind.AGENT:
            if agent_id is None:
                raise RuntimeError("Agent-principal grant row is missing its Agent")
            return AgentGrantPrincipal(agent_id=agent_id)
        case GrantPrincipalKind.ACCESS_PROFILE:
            if access_profile_id is None:
                raise RuntimeError("access-profile-principal grant row is missing its access profile")
            return AccessProfileGrantPrincipal(access_profile_id=access_profile_id)


def grant_principal_column_values(grant_principal: GrantPrincipal) -> tuple[UUID | None, AccessProfileId | None]:
    """Project a grant principal onto the relational principal columns."""

    match grant_principal:
        case AgentGrantPrincipal(agent_id=agent_id):
            return agent_id, None
        case AccessProfileGrantPrincipal(access_profile_id=access_profile_id):
            return None, access_profile_id


def grant_principal_applies_to(grant_principal: GrantPrincipal, request_principal: RequestPrincipal) -> bool:
    """Return whether ``grant_principal`` covers ``request_principal``."""

    match grant_principal:
        case AgentGrantPrincipal(agent_id=agent_id):
            return request_principal.agent_id == agent_id
        case AccessProfileGrantPrincipal(access_profile_id=access_profile_id):
            return request_principal.access_profile_id == access_profile_id
    assert_never(grant_principal)
