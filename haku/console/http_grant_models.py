"""Typed vocabulary for temporary HTTP egress grants.

One grant covers requests to one exact canonical public origin — ``(scheme, IDNA A-label host,
port)`` — narrowed by an explicit HTTP method set and an optional path regex. Whether a granted
hostname resolves to a permitted public address is the proxy adapter's SSRF/DNS-rebinding check at
connect time, never a property of the stored grant: this domain answers only who may send which
requests to which origin.

A grant's owner controls its lifecycle, its principal receives the permission, and its source
ToolCall remains immutable provenance rather than an authorization identity. Lifecycle status is
derived, never stored (root STYLE.md § SQLAlchemy): the row records the end facts —
``released_at``, ``revoked_at`` — and :func:`derive_status` computes the vocabulary from them and
the clock, so expiry needs no sweeper.
"""

from __future__ import annotations

import datetime
import ipaddress
import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import idna
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from haku.console.grant_principal import AgentGrantPrincipal, GrantPrincipal


class HttpGrantStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class HttpMethod(StrEnum):
    """Grantable request methods. CONNECT and TRACE are transport/diagnostic verbs the egress
    boundary never grants, so they are not vocabulary."""

    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"


# Shared by the Pydantic models and the JSONB column type: serialization sorts the set so one
# method set has one stored and wire form.
type HttpMethods = Annotated[frozenset[HttpMethod], Field(min_length=1), PlainSerializer(sorted, when_used="json")]

_NON_EMPTY = Annotated[str, Field(min_length=1)]
# The WHATWG URL parser reads a host whose final label is decimal or 0x-hex as an IPv4 address
# (its "ends in a number" check), so these shapes are IP literals in disguise.
_IPV4ISH_FINAL_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")


class HttpOrigin(BaseModel):
    """One exact canonical public origin.

    ``host`` accepts a Unicode or A-label hostname and canonicalizes it to the lowercase IDNA
    A-label form; everything else — wildcards, IP literals, URL syntax, a trailing dot — is
    rejected rather than normalized. ``port`` is explicit: the canonical origin triple carries
    no scheme-implied defaulting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: HttpScheme
    host: str = Field(description="Hostname, canonicalized to its IDNA A-label form.")
    port: int = Field(ge=1, le=65_535, description="Explicit port: 443 for standard https, 80 for http.")

    @field_validator("host")
    @classmethod
    def canonicalize_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            raise ValueError("host must not be empty")
        if "*" in host:
            raise ValueError("wildcard hosts are not grantable")
        if set(host) & set(":/?#@[]"):
            raise ValueError("host must be a bare hostname, not URL syntax or an IP literal")
        if host.endswith("."):
            raise ValueError("host must not carry a trailing dot")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP-literal origins are not grantable")
        try:
            canonical = idna.encode(host, uts46=True).decode("ascii")
        except idna.IDNAError as error:
            raise ValueError(f"host is not a valid IDNA hostname: {error}") from error
        if _IPV4ISH_FINAL_LABEL.fullmatch(canonical.rsplit(".", 1)[-1]):
            raise ValueError("IP-literal origins are not grantable")
        return canonical


class HttpGrantSpec(BaseModel):
    """One requested coverage item: an exact origin, its permitted methods, an optional path pin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: HttpOrigin
    methods: HttpMethods = Field(description="Request methods the grant permits at the origin.")
    path_regex: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Optional regex the request URL's path (query excluded) must fully match, e.g. "
            "'/repos/agentydragon/.*'. Absent means every path at the origin."
        ),
    )

    @field_validator("path_regex")
    @classmethod
    def compilable_path_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError(f"path_regex is not a valid regular expression: {error}") from error
        return value

    def covers(self, *, method: HttpMethod, path: str) -> bool:
        """Whether this coverage admits one request at its origin."""

        if method not in self.methods:
            return False
        return self.path_regex is None or re.fullmatch(self.path_regex, path) is not None


def derive_status(
    *,
    released_at: datetime.datetime | None,
    revoked_at: datetime.datetime | None,
    expires_at: datetime.datetime,
    now: datetime.datetime,
) -> HttpGrantStatus:
    """Compute the lifecycle vocabulary from the end facts and the clock.

    Expiration wins over an end action recorded at or after ``expires_at``, so a late release or
    revocation cannot revive or relabel a lease that had already reached its time bound.
    """

    if released_at is not None and released_at < expires_at:
        return HttpGrantStatus.RELEASED
    if revoked_at is not None and revoked_at < expires_at:
        return HttpGrantStatus.REVOKED
    return HttpGrantStatus.EXPIRED if now >= expires_at else HttpGrantStatus.ACTIVE


class HttpGrant(BaseModel):
    """Durable grant returned by the service; ``status`` is derived from the facts at read time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    owner_agent_id: UUID
    principal: GrantPrincipal
    source_tool_call_id: _NON_EMPTY
    spec: HttpGrantSpec
    status: HttpGrantStatus
    created_at: AwareDatetime
    expires_at: AwareDatetime
    released_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None
    end_reason: str | None = None

    @model_validator(mode="after")
    def validate_principal_owner(self) -> HttpGrant:
        # Session ownership is a relational invariant enforced while persisting/reconstructing the
        # grant: a globally unique session ID intentionally does not duplicate its Agent ID here.
        if isinstance(self.principal, AgentGrantPrincipal) and self.principal.agent_id != self.owner_agent_id:
            raise ValueError("Agent grant principals must belong to the lifecycle owner")
        return self

    @model_validator(mode="after")
    def validate_end_facts(self) -> HttpGrant:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.released_at is not None and self.revoked_at is not None:
            raise ValueError("a grant cannot be both released and revoked")
        ended = self.released_at is not None or self.revoked_at is not None
        if ended != (self.end_reason is not None and bool(self.end_reason.strip())):
            raise ValueError("end_reason travels exactly with a recorded end action")
        match self.status:
            case HttpGrantStatus.RELEASED if self.released_at is None:
                raise ValueError("a released grant requires released_at")
            case HttpGrantStatus.REVOKED if self.revoked_at is None:
                raise ValueError("a revoked grant requires revoked_at")
            case HttpGrantStatus.ACTIVE if ended:
                raise ValueError("an active grant cannot carry end facts")
            case _:
                pass
        return self


class HttpRequestAllowed(BaseModel):
    """An active grant covers the request; valid until the named expiry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[True] = True
    grant_id: UUID
    expires_at: AwareDatetime


class HttpRequestDenied(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[False] = False
    reason: _NON_EMPTY


type HttpGrantDecision = HttpRequestAllowed | HttpRequestDenied


class HttpGrantError(Exception):
    """Base class for grant-domain failures."""


class HttpGrantNotFoundError(HttpGrantError, LookupError):
    pass


class HttpGrantOwnershipError(HttpGrantError, PermissionError):
    pass


class HttpGrantSourceError(HttpGrantError, ValueError):
    pass
