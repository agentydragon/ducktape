"""Deploy-time credentials and configuration grants of the internal HTTP egress decide endpoint (#4670).

The ``egress_decide`` section of the console config file declares the decision endpoint token the
colocated egress proxy presents, the Console-owned egress credential registry (#4885): per handle,
the inert placeholder a sandbox presents, the headers the proxy scans for it, the principal
allowed to redeem it, and the exact origins it may be redeemed at — and the configuration-file
HTTP grants (#4941): the reviewed allowances ``HttpDecideService`` evaluates before database
grants. Pydantic overlays secret values directly at their typed leaves; handles, placeholders,
match headers, and grant entries are inert and committable by design (#4884 placeholder ruling).
The section also carries the deploy's ``prohibited_cidrs`` — inert address
policy the decide service enforces on resolved answers beyond its always-on prohibited classes
(#4948).
"""

from __future__ import annotations

import logging
import re
from ipaddress import IPv4Network, IPv6Network

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.grants.http.models import CREDENTIAL_HANDLE_PATTERN, HttpOrigin, HttpRequestCoverage, HttpScheme
from haku.console.grants.principal import ConfigGrantPrincipal

logger = logging.getLogger(__name__)

_HEADER_NAME = re.compile(r"[a-z0-9-]+")


class HttpOriginPattern(BaseModel):
    """One configuration-only origin pattern: exact scheme and port, host by regex fullmatch.

    For destination fleets whose hostname is not constant (e.g. GitHub's numbered
    ``productionresults*.blob.core.windows.net`` Actions log stores). Configuration-file
    capability only — credential entries and the Agent-requestable grant path stay exact-origin
    (`grants.http.models.HttpOrigin`), so no requested-and-approved grant can carry a pattern.
    The pattern fullmatches the request host in its canonical lowercase A-label form. Address
    policy is unchanged: a pattern admits no prohibited-address answer unless its entry carries
    ``allow_prohibited_address``, and credential substitution still binds to the credential
    entry's own exact origins. Pin the narrowest shape that covers the fleet, never a bare
    provider suffix an unrelated tenant can register under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: HttpScheme
    host_pattern: str = Field(
        min_length=1, max_length=256, description="Regex the canonical lowercase A-label request host must fullmatch."
    )
    port: int = Field(ge=1, le=65_535, description="Explicit port: 443 for standard https, 80 for http.")

    @field_validator("host_pattern")
    @classmethod
    def compilable(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError(f"host_pattern is not a valid regex: {error}") from error
        return value

    def matches(self, origin: HttpOrigin) -> bool:
        return (
            self.scheme == origin.scheme
            and self.port == origin.port
            and re.fullmatch(self.host_pattern, origin.host) is not None
        )


class EgressCredentialEntry(BaseModel):
    """One Console-owned egress credential, addressable from grants by its inert handle.

    A temporary HTTP grant redeems the credential by naming ``handle``
    (`grants.http.models.GrantSpec.credential_handle`); the decide endpoint then emits the
    ``placeholder`` → real-value substitution for requests the grant admits, but only for its
    ``principal`` at an origin in ``origins`` — the #4670 redemption binding. The secret ``value``
    is supplied through Pydantic's nested environment overlay; every other
    field is inert and safe to commit, log, and show to Agents.

    Presentation (``placeholder``, ``match_headers``) deliberately lives on this reviewed entry,
    not on grants: the Agent-driven grant path can only ever name a handle, so no grant — however
    shaped or mistakenly approved — can influence where or how the credential's bytes are
    injected. A bad grant's blast radius stays "may redeem this handle at its configured
    origins", never "may change its presentation". A credential needing a second presentation is
    a second entry receiving the same secret value under its own handle and placeholder.
    """

    handle: str = Field(
        max_length=64,
        pattern=CREDENTIAL_HANDLE_PATTERN,
        description="Audit-safe registry name grants redeem by, e.g. 'github-bot'.",
    )
    placeholder: str = Field(
        # The proxy applies the substitution as a substring swap inside the scanned headers, so a
        # short or generic placeholder could collide with legitimate header content.
        min_length=16,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=(
            "Deterministic string the sandbox presents in place of the credential, configurable "
            "per credential: default to an obviously-non-secret-on-sight style like "
            "'github-token-placeholder'; use a format-shaped string (e.g. ghp_-prefixed for "
            "GitHub) only where client software validates token format before sending. Either "
            "way it is inert — committable and loggable; only the redeemed value is secret."
        ),
    )
    value: SecretStr | None = Field(
        default=None,
        description="Real value overlaid by nested settings; absent leaves this presentation unprovisioned.",
    )
    match_headers: frozenset[str] = Field(
        min_length=1,
        description=(
            "Header names the proxy scans for the placeholder; it passes through anywhere else. "
            "Normalized to and compared in lowercase — HTTP/2 mandates lowercase field names, and "
            "HTTP/1 matching is case-insensitive anyway. Not limited to Authorization: an API-key "
            "credential names its own header, e.g. x-api-key."
        ),
    )
    principal: ConfigGrantPrincipal = Field(description="Principal allowed to redeem this credential.")
    origins: frozenset[HttpOrigin] = Field(
        min_length=1, description="Exact canonical origins the credential may be redeemed at."
    )

    @field_validator("match_headers", mode="after")
    @classmethod
    def canonicalize_match_headers(cls, value: frozenset[str]) -> frozenset[str]:
        headers = frozenset(header.lower() for header in value)
        for header in headers:
            if not _HEADER_NAME.fullmatch(header):
                raise ValueError(f"match header is not a valid header name: {header!r}")
        return headers


class EgressConfigGrantEntry(BaseModel):
    """One configuration-file HTTP grant: which principal may reach which exact origins, with
    which request coverage, and optionally which registry credential redeems there.

    Configuration grants and database grants (#4941) both hold an
    ``HttpRequestCoverage`` over exact canonical origins — an explicit method set and an optional
    fullmatch path-plus-query regex — evaluated before database grants, with configuration-file decision
    provenance ``config_file:<id>``. Reachability and credential redemption stay two typed authorities (#4670):
    a configuration grant only *names* a registry handle; presentation (placeholder, match headers) and
    the redemption binding (principal/origin allowlists) live on the ``EgressCredentialEntry`` itself,
    so no configuration grant can alter where or how a credential's bytes are injected. Entries may
    overlap: the first declared match names the decision, and every matching entry's credential
    redeems.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
        description="Stable audit name this entry's admissions carry as decision provenance.",
    )
    principal: ConfigGrantPrincipal = Field(description="Principal this grant applies to.")
    origins: frozenset[HttpOrigin] = Field(
        default_factory=frozenset, description="Exact canonical origins the Agents may reach under this entry."
    )
    origin_patterns: frozenset[HttpOriginPattern] = Field(
        default_factory=frozenset,
        description=(
            "Host-pattern origins evaluated alongside `origins` — the configuration-only "
            "capability for non-constant destination fleets (`HttpOriginPattern`)."
        ),
    )
    coverage: HttpRequestCoverage = Field(description="Method set and optional path pin at each origin.")
    credential_handle: str | None = Field(
        default=None,
        max_length=64,
        pattern=CREDENTIAL_HANDLE_PATTERN,
        description=(
            "Registry credential (`egress_decide.credentials`) redeemed for requests this grant "
            "admits, subject to the registry entry's own principal/origin binding. Absent, the grant "
            "is pure reachability."
        ),
    )
    allow_prohibited_address: bool = Field(
        default=False,
        description=(
            "Capability: when set, requests this entry admits may reach its origins even when the "
            "host resolves entirely into otherwise-prohibited address space — the always-prohibited "
            "classes or a deploy `prohibited_cidrs` entry (`decide_service`). Scoped to this entry's "
            "own origins, never a global private-address allow; a mixed public+internal answer stays "
            "denied as a rebinding signature. Default False keeps the private-address boundary. This "
            "is the reviewed primitive for granting an Agent one exact cluster-internal destination "
            "(e.g. an in-cluster model gateway)."
        ),
    )

    @model_validator(mode="after")
    def _names_some_origin(self) -> EgressConfigGrantEntry:
        if not self.origins and not self.origin_patterns:
            raise ValueError("a configuration grant names at least one origin or origin pattern")
        return self

    def matches_origin(self, origin: HttpOrigin) -> bool:
        return origin in self.origins or any(pattern.matches(origin) for pattern in self.origin_patterns)


class EgressDecideConfig(BaseModel):
    """Wiring for ``POST /api/internal/http/decide``: the decision endpoint token the colocated egress
    proxy presents, the egress credential registry grants and configuration grants redeem from, and
    the configuration-file HTTP grants themselves.
    ``None`` on ``ConsoleConfigFile`` is the production-safe default — the endpoint stays 503
    and the proxy fails closed until a deploy deliberately wires it."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    decision_endpoint_token: SecretStr
    credentials: dict[str, EgressCredentialEntry] = Field(default_factory=dict)
    grants: list[EgressConfigGrantEntry] = Field(default_factory=list)
    prohibited_cidrs: frozenset[IPv4Network | IPv6Network] = Field(
        default_factory=frozenset,
        description=(
            "Deploy-specific address ranges denied in resolved DNS answers on top of the "
            "always-prohibited classes the decide service enforces (loopback, link-local, "
            "multicast, unspecified, private/RFC1918/ULA): name the cluster's service/pod "
            "CIDRs and any other reachable-but-internal ranges here. Inert and committable "
            "— ranges, not secrets."
        ),
    )

    @model_validator(mode="after")
    def _coherent_credential_registry(self) -> EgressDecideConfig:
        for slot in self.credentials:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
                raise ValueError(f"invalid egress credential slot {slot!r}")
        handles = [entry.handle for entry in self.credentials.values()]
        if len(set(handles)) != len(handles):
            raise ValueError("egress credential handles must be distinct")
        placeholders = [entry.placeholder for entry in self.credentials.values()]
        # Substitutions are substring swaps, so one placeholder containing another would make the
        # emitted substitution set order-dependent and corrupting.
        for placeholder in placeholders:
            if sum(1 for other in placeholders if placeholder in other) > 1:
                raise ValueError("no egress credential placeholder may contain another")
        return self

    @model_validator(mode="after")
    def _coherent_grants(self) -> EgressDecideConfig:
        # Overlapping coverage is deliberately legal — a broad reachability entry beside a narrower
        # credentialed one is a natural reviewed config, and regex path scopes make overlap
        # undecidable in general — but ids must be unique for provenance and every named handle
        # must exist: a dangling reference in a reviewed file is a mistake, not a degrade case.
        ids = [entry.id for entry in self.grants]
        if len(set(ids)) != len(ids):
            raise ValueError("configuration grant entry ids must be distinct")
        handles = {entry.handle for entry in self.credentials.values()}
        for entry in self.grants:
            if entry.credential_handle is not None and entry.credential_handle not in handles:
                raise ValueError(
                    f"configuration grant entry {entry.id} names unknown credential handle {entry.credential_handle}"
                )
        return self


class LoadedEgressCredential(BaseModel):
    """An egress credential registry entry after reading its env reference."""

    handle: str
    placeholder: str
    value: SecretStr
    match_headers: frozenset[str]
    principal: ConfigGrantPrincipal
    origins: frozenset[HttpOrigin]


class LoadedEgressDecide(BaseModel):
    """The decide endpoint's credentials after reading env references.

    Configuration grants carry no secrets, so they pass through from the config unchanged —
    the evaluator's provenance names the literal reviewed entry.
    """

    decision_endpoint_token: SecretStr
    credentials: list[LoadedEgressCredential] = Field(default_factory=list)
    grants: list[EgressConfigGrantEntry] = Field(default_factory=list)


def load_egress_decide(config: EgressDecideConfig) -> LoadedEgressDecide:
    """Validate the decide endpoint's typed secrets.

    The endpoint authentication secret is required by the settings model. A registry credential
    (``config.credentials``) whose value is absent is instead
    skipped with a warning, and the endpoint still serves reachability verdicts and every other
    credential. This is fail-safe, not fail-open: a fenced sandbox only ever holds the inert
    placeholder, so a request whose credential was skipped simply sends the placeholder upstream —
    which the upstream rejects — and nothing leaks.

    A present egress credential value may not equal the endpoint-authentication secret. Egress
    credential values may repeat among themselves (two presentations of one credential), but a
    value equal to any configured placeholder would make the "inert" placeholder itself the
    secret, so that is refused the same way.
    """
    decision_endpoint_token = config.decision_endpoint_token.get_secret_value()
    identity_tokens = {decision_endpoint_token}
    placeholders = {entry.placeholder for entry in config.credentials.values()}
    loaded_credentials: list[LoadedEgressCredential] = []
    for slot, credential in config.credentials.items():
        if credential.value is None:
            # Skipped, not fatal (see docstring): fail-safe because the sandbox only ever holds the
            # inert placeholder, so a request that would have redeemed this credential passes the
            # placeholder through unchanged and it is worthless upstream (#4884 ruling).
            logger.warning("skipping unprovisioned egress credential %s in slot %s", credential.handle, slot)
            continue
        value = credential.value.get_secret_value()
        if value in identity_tokens:
            raise RuntimeError("duplicate egress decide credential values")
        if value in placeholders:
            raise RuntimeError(f"egress credential {credential.handle} value equals a configured placeholder")
        loaded_credentials.append(
            LoadedEgressCredential(
                handle=credential.handle,
                placeholder=credential.placeholder,
                value=credential.value,
                match_headers=credential.match_headers,
                principal=credential.principal,
                origins=credential.origins,
            )
        )
    return LoadedEgressDecide(
        decision_endpoint_token=config.decision_endpoint_token, credentials=loaded_credentials, grants=config.grants
    )
