"""Vocabulary of the per-request egress decision call.

One decision call carries both the reachability verdict and the request-specific
credential-substitution operations (github.com/agentydragon/ducktape/issues/4670).
The wire models below (``DecideRequest``/``DecideResponse``) are the schema of
Console's ``POST /api/internal/http/decide``, shared with the proxy adapter as an
internal same-release contract: proxy and Console deploy from one commit, so
there is no version negotiation and no versioning.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer, SecretStr, model_validator


class RequestMeta(BaseModel):
    """What the proxy tells the decision endpoint about one request or CONNECT.

    ``scheme``/``host``/``port`` are the connection target the proxy would dial —
    the Host header is client-controlled and never policy input. Bodies stay out
    of decision calls (#4670).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str = Field(description="HTTP method; CONNECT for tunnel establishment.")
    scheme: str | None = Field(description="Target scheme; None for CONNECT, where no inner request exists yet.")
    host: str = Field(description="Connection target host.")
    port: int = Field(description="Connection target port.")
    path: str | None = Field(description="Request path including query; None for CONNECT.")


class PlaceholderSubstitution(BaseModel):
    """Swap an inert placeholder for the real credential, iron-proxy replace-style.

    The sandbox holds a deterministic, inert placeholder instead of the
    credential: applying the substitution replaces each occurrence of
    ``placeholder`` within the scanned headers by ``value``, reaching inside
    the base64 payload of ``Basic`` credentials (the shape git over HTTPS
    sends). The placeholder is the capability handle — a request that never
    presents it is forwarded untouched and receives no credential — and it is
    worthless against the upstream, so occurrences this decision does not
    substitute (unscanned positions, routes the decider left unplaceholded)
    pass through verbatim rather than being stripped or refused. Destination
    scoping is deliberately absent here: decisions are per-request, so the
    decider returns a substitution only on requests it already checked against
    destination and Agent policy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    placeholder: str = Field(
        min_length=1,
        description="Inert, well-known value the sandbox presents in place of the credential; safe to log or commit.",
    )
    value: str = Field(
        min_length=1, description="Real credential swapped in for the placeholder; never log, cache, or persist it."
    )
    match_headers: frozenset[str] = Field(
        min_length=1,
        description="Header names (case-insensitive) scanned for the placeholder; it passes through anywhere else.",
    )


class DecisionSource(StrEnum):
    """Authority that produced the effective decision, for audit provenance."""

    STANDING = "standing"
    GRANT = "grant"
    NONE = "none"


def _sorted_addresses(addresses: Iterable[IPv4Address | IPv6Address]) -> list[str]:
    return [str(address) for address in sorted(addresses, key=lambda address: (address.version, int(address)))]


def _secret_value_or_none(value: SecretStr | None) -> str | None:
    return None if value is None else value.get_secret_value()


type ResolvedAddresses = Annotated[
    frozenset[IPv4Address | IPv6Address],
    # Bounded complete resolution: the proxy authorizes every address the answer contained, and an
    # oversized answer is refused rather than truncated (#4670). Serialization sorts so one answer
    # set has one wire form.
    Field(min_length=1, max_length=64),
    PlainSerializer(_sorted_addresses, when_used="json"),
]


class DecideRequest(BaseModel):
    """What the proxy asks Console about one admission: caller, pinned resolution, request meta.

    Header values and bodies stay out of the call: the placeholder the sandbox holds is inert, so
    nothing about it needs to travel inward — grant evaluation alone decides which substitutions
    come back — and Console sits on the request-decision path, never the body path (#4670). The
    ``proxy_client_credential`` is the required sandbox-to-proxy credential. Console-launched
    sandboxes use their existing bridge bearer here, so MCP and HTTP share one session credential;
    it is the sole source of Agent/session identity for this decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: RequestMeta
    resolved_ips: ResolvedAddresses = Field(
        description="Complete validated DNS answer for the request host, as the proxy resolved it."
    )
    upstream_ip: IPv4Address | IPv6Address = Field(
        description="The one resolved address the proxy pinned for the actual upstream connection."
    )
    proxy_client_credential: Annotated[SecretStr | None, PlainSerializer(_secret_value_or_none, when_used="json")] = (
        Field(
            default=None,
            description=(
                "The sandbox-to-proxy bearer, normally the same HAKU_RUNNER_TOKEN used by "
                "the MCP bridge. Console resolves it to a live Agent session when present."
            ),
        )
    )

    @model_validator(mode="after")
    def upstream_ip_must_be_resolved(self) -> DecideRequest:
        if self.upstream_ip not in self.resolved_ips:
            raise ValueError("upstream_ip must be one of resolved_ips")
        return self


class GrantScope(BaseModel):
    """The exact canonical origin a temporary-grant request must name to cover the denied request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: str
    host: str
    port: int


class DecideAllowed(BaseModel):
    """Forward after applying the substitutions; a new admission needs a new decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[True] = True
    source: Literal[DecisionSource.STANDING, DecisionSource.GRANT] = Field(
        description="Which authority admitted the request: standing policy or a temporary grant."
    )
    decision_id: str = Field(
        min_length=1, description="Links audit records to the decision's policy provenance, e.g. 'grant:<grant UUID>'."
    )
    valid_until: AwareDatetime | None = Field(
        default=None,
        description=(
            "Exact admission deadline: a later request, CONNECT, or reconnect needs a fresh decision. "
            "An already admitted flow may overrun it only within the deployment's hard flow lifetime. "
            "None for a standing-policy admission, which has no deadline short of a config change — "
            "and a config change redeploys Console and proxy together; the hard flow lifetime still "
            "bounds admitted flows."
        ),
    )
    substitutions: list[PlaceholderSubstitution] = Field(
        default_factory=list,
        description="Already scoped to this one request; applied in order before forwarding, empty forwards as-is.",
    )


class DecideDenied(BaseModel):
    """Refuse without contacting the upstream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: Literal[False] = False
    source: Literal[DecisionSource.NONE] = DecisionSource.NONE
    reason: str = Field(min_length=1, description="Operator-facing denial reason; safe to log and to surface.")
    grant_scope: GrantScope | None = Field(
        default=None,
        description=(
            "Canonical origin to name in a grant request covering this request; absent when no "
            "grantable scope exists (unknown caller, ungrantable origin)."
        ),
    )


type DecideResponse = DecideAllowed | DecideDenied
