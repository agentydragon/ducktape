"""Typed vocabulary for temporary HTTP egress grants.

One grant covers requests to one exact canonical public origin — ``(scheme, IDNA A-label host,
port)`` — narrowed by an explicit HTTP method set and an optional path regex. Whether a granted
hostname resolves to a permitted public address is the proxy adapter's SSRF/DNS-rebinding check at
connect time, never a property of the stored grant: this domain answers only who may send which
requests to which origin. A grant may additionally name the Console-owned credential it redeems
at that origin by inert config-registry handle (#4885); credential values live in deployment env
references (`decide_config`), never in this domain or in Postgres.

The grant's envelope — owner, principal, provenance, validity window — is the shared
`haku.console.grants.envelope`. Lifecycle status is derived, never stored (root STYLE.md
§ SQLAlchemy): the row records the end fact — ``ended_at`` — and the
envelope's :func:`~haku.console.grants.envelope.derive_status` computes the vocabulary from
them and the clock, so expiry needs no sweeper.
"""

from __future__ import annotations

import datetime
import ipaddress
import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import idna
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, computed_field, field_validator

from haku.console.grants.envelope import NON_EMPTY, GrantEnvelope, GrantStatus, derive_status

# One spelling for the inert credential-handle slug, shared with the deploy-config registry
# (`decide_config.EgressCredentialEntry.handle`) that grants redeem from.
CREDENTIAL_HANDLE_PATTERN = r"^[a-z][a-z0-9-]*$"


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


class HttpRequestCoverage(BaseModel):
    """The request-matching half of an allowance at an already-matched origin: a method set plus
    an optional path pin. Held as a field by temporary grants (`GrantSpec`) and deploy-managed
    standing policy entries (`decide_config.EgressStandingPolicyEntry`) so both speak one
    matcher vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    methods: HttpMethods = Field(description="Request methods the allowance permits at its origin.")
    path_regex: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Optional regex the request path plus query — exactly as the proxy sends it — must "
            "fully match, e.g. '/repos/agentydragon/.*'. Absent means every path at the origin."
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


class GrantSpec(BaseModel):
    """One requested coverage item: an exact origin, the request coverage at it, and optionally
    the Console-owned credential the grant redeems there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: HttpOrigin
    coverage: HttpRequestCoverage = Field(description="Method set and optional path pin at the origin.")
    credential_handle: str | None = Field(
        default=None,
        max_length=64,
        pattern=CREDENTIAL_HANDLE_PATTERN,
        description=(
            "Console-owned egress credential this grant redeems at its origin, named by its "
            "deploy-config handle (`egress_decide.credentials`). The sandbox holds only the "
            "credential's inert placeholder; the real value is substituted at the egress proxy "
            "and never reaches the Agent. Absent, the grant is pure reachability."
        ),
    )
    allow_prohibited_address: bool = Field(
        default=False,
        description=(
            "Capability: when set, requests this grant admits may reach its origin even when the "
            "host resolves entirely into otherwise-prohibited address space — the decide oracle's "
            "always-prohibited classes or a deploy `prohibited_cidrs` entry (`decide_service`). "
            "Scoped to this grant's own origin, never a global private-address allow; a mixed "
            "public+internal answer stays denied as a rebinding signature. Default False keeps the "
            "private-address boundary. This is the reusable primitive for reaching one exact "
            "cluster-internal destination; set it only on operator-approved grants."
        ),
    )


class Grant(GrantEnvelope):
    """Durable grant returned by the service: the shared envelope plus origin/coverage spec.

    ``status`` is computed from the envelope's recorded end facts and the clock at access
    time (`haku.console.grants.envelope.derive_status`) — never stored and never a field, so
    it cannot disagree with the facts.
    """

    spec: GrantSpec

    # The ignore is pydantic's documented mypy accommodation for computed_field-on-property
    # (mypy's prop-decorator limitation), not a silenced finding.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> GrantStatus:
        return derive_status(
            ended_at=self.ended_at, expires_at=self.expires_at, now=datetime.datetime.now(datetime.UTC)
        )


class HttpRequestAllowed(BaseModel):
    """An active grant covers the request; valid until the named expiry.

    ``credential_handles`` carries every credential named by a matching grant — handles are
    inert config-registry names, never values. Whether a handle actually redeems into a
    substitution is the decide layer's separate credential-authority evaluation
    (`decide_service`), so this decision alone never moves a secret.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[True] = True
    grant_id: UUID
    expires_at: AwareDatetime
    credential_handles: Annotated[frozenset[str], PlainSerializer(sorted, when_used="json")] = Field(
        default_factory=frozenset,
        description="Credential handles named by every matching grant; empty for pure reachability.",
    )


class HttpRequestDenied(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[False] = False
    reason: NON_EMPTY


type GrantDecision = HttpRequestAllowed | HttpRequestDenied
