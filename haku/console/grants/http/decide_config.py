"""Deploy-time credentials and configuration grants of the internal HTTP egress decide endpoint (#4670).

The ``egress_decide`` section of the console config file declares the shared-fence bearer the
colocated egress proxy presents, the Console-owned egress credential registry (#4885): per handle,
the inert placeholder a sandbox presents, the headers the proxy scans for it, the principal
allowed to redeem it, and the exact origins it may be redeemed at — and the configuration-file
HTTP grants (#4941): the reviewed allowances ``HttpDecideService`` evaluates before database
grants. Secret values stay
in env-var references like every other console-only bearer; handles, placeholders, match headers,
and grant entries are inert and committable by design (#4884 placeholder ruling).
``load_egress_decide`` reads the references once at startup and hands ``HttpDecideService`` the
resolved values. The section also carries the deploy's ``prohibited_cidrs`` — inert address
policy the decide service enforces on resolved answers beyond its always-on prohibited classes
(#4948).
"""

from __future__ import annotations

import logging
import os
import re
from ipaddress import IPv4Network, IPv6Network

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.grants.http.models import CREDENTIAL_HANDLE_PATTERN, HttpOrigin, HttpRequestCoverage
from haku.console.grants.principal import ConfigGrantPrincipal
from util.env import EnvironmentVariableName

logger = logging.getLogger(__name__)

_HEADER_NAME = re.compile(r"[a-z0-9-]+")


class EgressCredentialEntry(BaseModel):
    """One Console-owned egress credential, addressable from grants by its inert handle.

    A temporary HTTP grant redeems the credential by naming ``handle``
    (`grants.http.models.GrantSpec.credential_handle`); the decide endpoint then emits the
    ``placeholder`` → real-value substitution for requests the grant admits, but only for its
    ``principal`` at an origin in ``origins`` — the #4670 redemption binding. Everything
    here except the env-referenced value is inert: safe to commit, log, and show to Agents.

    Presentation (``placeholder``, ``match_headers``) deliberately lives on this reviewed entry,
    not on grants: the Agent-driven grant path can only ever name a handle, so no grant — however
    shaped or mistakenly approved — can influence where or how the credential's bytes are
    injected. A bad grant's blast radius stays "may redeem this handle at its configured
    origins", never "may change its presentation". A credential needing a second presentation is
    a second entry sharing the same ``value_env_var`` under its own handle and placeholder.
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
    value_env_var: EnvironmentVariableName = Field(
        description="Env reference holding the real value; two presentations of one credential share it."
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
        min_length=1, description="Exact canonical origins the Agents may reach under this entry."
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


class EgressDecideConfig(BaseModel):
    """Wiring for ``POST /api/internal/http/decide``: the shared-fence bearer the colocated egress
    proxy presents, the egress credential registry grants and configuration grants redeem from, and
    the configuration-file HTTP grants themselves.
    ``None`` on ``ConsoleConfigFile`` is the production-safe default — the endpoint stays 503
    and the proxy fails closed until a deploy deliberately wires it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fence_credential_env_var: EnvironmentVariableName
    credentials: list[EgressCredentialEntry] = Field(default_factory=list)
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
    def _distinct_env_references(self) -> EgressDecideConfig:
        identity_env_vars = [self.fence_credential_env_var]
        # Credential entries may share a value_env_var with each other — that is how one credential
        # carries a second presentation — but never with an identity secret.
        if set(identity_env_vars) & {entry.value_env_var for entry in self.credentials}:
            raise ValueError("egress credential env vars must not reference identity secrets")
        return self

    @model_validator(mode="after")
    def _coherent_credential_registry(self) -> EgressDecideConfig:
        handles = [entry.handle for entry in self.credentials]
        if len(set(handles)) != len(handles):
            raise ValueError("egress credential handles must be distinct")
        placeholders = [entry.placeholder for entry in self.credentials]
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
        handles = {entry.handle for entry in self.credentials}
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

    fence_credential: SecretStr
    credentials: list[LoadedEgressCredential] = Field(default_factory=list)
    grants: list[EgressConfigGrantEntry] = Field(default_factory=list)


def load_egress_decide(config: EgressDecideConfig) -> LoadedEgressDecide:
    """Read the decide endpoint's env-referenced secrets.

    The endpoint authentication secret fails loud at startup: an unset fence-credential var
    raises, because without it the colocated fence cannot authenticate. A registry credential
    (``config.credentials``) whose value var is unset is instead
    skipped with a warning, and the endpoint still serves reachability verdicts and every other
    credential. This is fail-safe, not fail-open: a fenced sandbox only ever holds the inert
    placeholder, so a request whose credential was skipped simply sends the placeholder upstream —
    which the upstream rejects — and nothing leaks.

    A present egress credential value may not equal the endpoint-authentication secret. Egress
    credential values may repeat among themselves (two presentations of one credential), but a
    value equal to any configured placeholder would make the "inert" placeholder itself the
    secret, so that is refused the same way.
    """
    fence_credential = os.environ.get(config.fence_credential_env_var)
    if not fence_credential:
        raise RuntimeError(f"missing fence credential env var {config.fence_credential_env_var}")
    identity_tokens = {fence_credential}
    placeholders = {entry.placeholder for entry in config.credentials}
    loaded_credentials: list[LoadedEgressCredential] = []
    for credential in config.credentials:
        value = os.environ.get(credential.value_env_var)
        if not value:
            # Skipped, not fatal (see docstring): fail-safe because the sandbox only ever holds the
            # inert placeholder, so a request that would have redeemed this credential passes the
            # placeholder through unchanged and it is worthless upstream (#4884 ruling).
            logger.warning(
                "skipping egress credential %s: value env var %s is unset", credential.handle, credential.value_env_var
            )
            continue
        if value in identity_tokens:
            raise RuntimeError("duplicate egress decide credential values")
        if value in placeholders:
            raise RuntimeError(f"egress credential {credential.handle} value equals a configured placeholder")
        loaded_credentials.append(
            LoadedEgressCredential(
                handle=credential.handle,
                placeholder=credential.placeholder,
                value=SecretStr(value),
                match_headers=credential.match_headers,
                principal=credential.principal,
                origins=credential.origins,
            )
        )
    return LoadedEgressDecide(
        fence_credential=SecretStr(fence_credential), credentials=loaded_credentials, grants=config.grants
    )
