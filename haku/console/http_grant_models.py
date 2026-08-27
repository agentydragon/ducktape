"""Typed vocabulary for temporary HTTP egress grants.

The grant unit is one exact canonical public origin — ``(scheme, IDNA A-label host, port)``.
V1 deliberately has no wildcard, path, method, regex, or IP-literal grant scopes. Whether a
granted hostname resolves to a permitted public address is the proxy adapter's SSRF/DNS-rebinding
check at connect time, never a property of the stored grant: this domain answers only who may
reach which origin.

A grant's owner controls its lifecycle, its principal receives the permission, and its source
ToolCall remains immutable provenance rather than an authorization identity.
"""

from __future__ import annotations

import datetime
import ipaddress
import re
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import idna
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from haku.console.grant_principal import AgentGrantPrincipal, GrantPrincipal


class HttpGrantStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    EXPIRED = "expired"


class HttpScheme(StrEnum):
    HTTP = "http"
    HTTPS = "https"


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


class HttpGrant(BaseModel):
    """Durable grant returned by the service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    owner_agent_id: UUID
    principal: GrantPrincipal
    source_tool_call_id: _NON_EMPTY
    origin: HttpOrigin
    status: HttpGrantStatus
    created_at: datetime.datetime
    expires_at: datetime.datetime
    ended_at: datetime.datetime | None = None
    end_reason: str | None = None

    @field_validator("created_at", "expires_at", "ended_at")
    @classmethod
    def validate_timezone_aware(cls, value: datetime.datetime | None, info: ValidationInfo) -> datetime.datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_principal_owner(self) -> HttpGrant:
        # Session ownership is a relational invariant enforced while persisting/reconstructing the
        # grant: a globally unique session ID intentionally does not duplicate its Agent ID here.
        if isinstance(self.principal, AgentGrantPrincipal) and self.principal.agent_id != self.owner_agent_id:
            raise ValueError("Agent grant principals must belong to the lifecycle owner")
        return self

    @model_validator(mode="after")
    def validate_timestamps(self) -> HttpGrant:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.status is HttpGrantStatus.ACTIVE:
            if self.ended_at is not None or self.end_reason is not None:
                raise ValueError("an active grant cannot have terminal fields")
        elif self.ended_at is None or not self.end_reason or not self.end_reason.strip():
            raise ValueError("a terminal grant requires ended_at and a non-empty end_reason")
        return self


class HttpGrantDecision(BaseModel):
    """Result of matching one origin against a request principal's currently active grants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    grant_id: UUID | None = None
    expires_at: datetime.datetime | None = None
    reason: str | None = None


class HttpGrantError(Exception):
    """Base class for grant-domain failures."""


class HttpGrantNotFoundError(HttpGrantError, LookupError):
    pass


class HttpGrantOwnershipError(HttpGrantError, PermissionError):
    pass


class HttpGrantSourceError(HttpGrantError, ValueError):
    pass
