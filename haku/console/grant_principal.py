"""Shared principal vocabulary for time-boxed Console grants.

A grant principal answers only who receives and may exercise a permission. Source
ToolCall provenance, requester identity, and lifecycle ownership remain separate.

Callers construct :class:`RequestPrincipal` only from authenticated runtime identity.
A session ID is usable only after the authentication boundary has confirmed that the
live session belongs to the named Agent; request payloads never select principal IDs.
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
    SESSION = "session"


class AgentGrantPrincipal(BaseModel):
    """Every authenticated execution of one Agent receives the grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantPrincipalKind.AGENT] = GrantPrincipalKind.AGENT
    agent_id: UUID


class SessionGrantPrincipal(BaseModel):
    """Only one exact authenticated live session receives the grant.

    A Console session is already pinned to one Agent, so the globally unique session
    ID is the complete narrow principal. Creation provenance validates that relationship.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[GrantPrincipalKind.SESSION] = GrantPrincipalKind.SESSION
    session_id: UUID


type GrantPrincipal = Annotated[AgentGrantPrincipal | SessionGrantPrincipal, Field(discriminator="kind")]


class RequestPrincipal(BaseModel):
    """Complete trusted authenticated identity attempting to exercise a grant or SAR.

    When ``session_id`` is present, the authentication boundary must already have
    verified that the globally unique session belongs to ``agent_id``. The access
    profile remains standing-policy context; it is not a temporary grant principal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    session_id: UUID | None
    access_profile_id: AccessProfileId | None

    @classmethod
    def from_source(cls, source: AgentActor) -> RequestPrincipal:
        """Project the bearer-authenticated Agent identity into the request-principal vocabulary,
        dropping the operator and credential-binding identity that applicability must not read."""

        return cls(agent_id=source.agent_id, session_id=source.session_id, access_profile_id=source.access_profile_id)


def grant_principal_applies_to(grant_principal: GrantPrincipal, request_principal: RequestPrincipal) -> bool:
    """Return whether ``grant_principal`` covers ``request_principal``.

    A session request principal inherits its Agent's grants. Static credentials have no
    session identity and therefore cannot exercise exact-session grants.
    """

    match grant_principal:
        case AgentGrantPrincipal(agent_id=agent_id):
            return request_principal.agent_id == agent_id
        case SessionGrantPrincipal(session_id=session_id):
            return request_principal.session_id == session_id
    assert_never(grant_principal)
