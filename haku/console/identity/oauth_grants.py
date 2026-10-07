"""Transport-neutral records for binding-backed Agent OAuth grants."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from haku.console.tool_call_actor import AgentActor


@dataclass(frozen=True, slots=True)
class AuthorizationCorrelation:
    """Validated OAuth values that locate one enrollment attempt."""

    client_id: str
    redirect_uri: str
    code_challenge: str


@dataclass(frozen=True, slots=True)
class ClientSoftwareSnapshot:
    """Client presentation metadata captured for the consent interaction."""

    client_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Immutable request data persisted after the upstream accepts authorization."""

    correlation: AuthorizationCorrelation
    client: ClientSoftwareSnapshot
    requested_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class GrantAuthorization:
    """A current binding-backed resolution of a Haku grant."""

    grant_id: UUID
    actor: AgentActor
    client_id: str
    allowed_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class TokenFamilyEvidence:
    """Receipt proving the upstream persisted the initial local token family."""

    access_jti: str
    refresh_jti: str | None


class DuplicateAuthorizationError(Exception):
    """The same live or tombstoned OAuth correlation is already reserved."""


class EnrollmentRejectedError(Exception):
    """The enrollment interaction cannot authorize this terminal exchange."""


class ExchangeAlreadyClaimedError(Exception):
    """Another exchange request already won the interaction transition."""


class GrantRejectedError(Exception):
    """The referenced Haku grant/binding is not authorized for this operation."""


class AgentGrantAuthorityUnavailableError(Exception):
    """Haku cannot currently make an authoritative grant decision."""
