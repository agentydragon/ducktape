"""Vocabulary of the per-request egress decision call.

One decision call carries both the reachability verdict and the request-specific
credential-substitution operations (github.com/agentydragon/ducktape/issues/4670).
The wire models below (``DecideRequest``/``HttpAuthorizationDecision``) are the schema of
Console's ``POST /api/internal/http/decide``, shared with the proxy adapter as an
internal same-release contract: proxy and Console are one pod speaking over its
loopback, with no version negotiation and no versioning. The one skew this file
must still absorb is the two containers' image tags landing in separate Flux
automation commits, which re-rolls the pod minutes apart with one side ahead —
the ``session_token`` wire aliases below exist for exactly that window.
"""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    model_validator,
)

from haku.grants.authorization import AuthorizationAllowed, AuthorizationDenied


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


def _sorted_addresses(addresses: Iterable[IPv4Address | IPv6Address]) -> list[str]:
    return [str(address) for address in sorted(addresses, key=lambda address: (address.version, int(address)))]


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
    ``session_token`` is the required per-Session secret the sandbox presented as proxy
    authentication — the same HAKU_SESSION_TOKEN the runner protocol and Console MCP authenticate —
    and it is the sole source of Agent/session identity for this decision.
    """

    # serialize_by_alias makes every dump apply the session_token serialization_alias below; it
    # leaves the other fields untouched (no aliases) and goes with the CLEANUP there.
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True)

    request: RequestMeta
    resolved_ips: ResolvedAddresses = Field(
        description="Complete validated DNS answer for the request host, as the proxy resolved it."
    )
    upstream_ip: IPv4Address | IPv6Address = Field(
        description="The one resolved address the proxy pinned for the actual upstream connection."
    )
    session_token: Annotated[SecretStr, PlainSerializer(SecretStr.get_secret_value, when_used="json")] = Field(
        # CLEANUP(added 2026-08-29): proxy_client_credential is the pre-rename wire spelling.
        # Serialization keeps it so a proxy image one automation commit ahead of its pod's Console
        # image (or behind) never hits extra="forbid" — a deny-all window, since the gate fails
        # closed. One release after both images converge: drop serialization_alias (and the
        # serialize_by_alias in model_config) so the wire says session_token; the release after
        # that, drop the validation alias.
        validation_alias=AliasChoices("session_token", "proxy_client_credential"),
        serialization_alias="proxy_client_credential",
        description=(
            "The caller's session token, presented to the proxy as proxy authentication. "
            "Console resolves it to a live Agent session."
        ),
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


class HttpAuthorizationAllowed(AuthorizationAllowed):
    """Forward after applying the substitutions; a new admission needs a new decision."""

    valid_until: AwareDatetime | None = Field(
        default=None,
        description=(
            "Exact admission deadline: a later request, CONNECT, or reconnect needs a fresh decision. "
            "None means the authority has no end date; the deployment's hard flow lifetime still bounds flows."
        ),
    )
    substitutions: list[PlaceholderSubstitution] = Field(
        default_factory=list,
        description="Already scoped to this one request; applied in order before forwarding, empty forwards as-is.",
    )


class HttpAuthorizationDenied(AuthorizationDenied):
    """Refuse without contacting the upstream."""

    grant_scope: GrantScope | None = Field(
        default=None,
        description=(
            "Canonical origin to name in a grant request covering this request; absent when no "
            "grantable scope exists (unknown caller, ungrantable origin)."
        ),
    )


type HttpAuthorizationDecision = HttpAuthorizationAllowed | HttpAuthorizationDenied
