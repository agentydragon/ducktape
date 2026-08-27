"""Deploy-time credentials of the internal HTTP egress decide endpoint (#4670).

The ``egress_decide`` section of the console config file declares which bearer the colocated
egress proxy presents, which Agent-bound fence credentials the endpoint resolves, and the
Console-owned egress credential registry (#4885): per handle, the inert placeholder a sandbox
presents, the headers the proxy scans for it, the Agents allowed to redeem it, and the exact
origins it may be redeemed at. Secret values stay in env-var references like every other
console-only bearer; handles, placeholders, and match headers are inert and committable by
design (#4884 placeholder ruling). ``load_egress_decide`` reads the references once at startup
and hands ``HttpDecideService`` the resolved values.
"""

from __future__ import annotations

import os
import re
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from haku.console.http_grant_models import HttpOrigin

_HEADER_NAME = re.compile(r"[a-z0-9-]+")


class EgressFenceCredentialEntry(BaseModel):
    """One Agent-bound fence credential the internal HTTP egress decide endpoint accepts.

    Endpoint-scoped by construction: the referenced secret is resolved only by the decide service,
    never registered with the general Agent bearer authority, so it is invalid for MCP, session,
    and operator APIs. Deploy config binds each env-referenced secret to its Agent until minted
    per-claim fence credentials (#4670's fence-credential work item) replace this static wiring.
    """

    agent_id: UUID
    token_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")


class EgressCredentialEntry(BaseModel):
    """One Console-owned egress credential, addressable from grants by its inert handle.

    A temporary HTTP grant redeems the credential by naming ``handle``
    (`http_grant_models.HttpGrantSpec.credential_handle`); the decide endpoint then emits the
    ``placeholder`` → real-value substitution for requests the grant admits, but only for an
    Agent in ``agent_ids`` at an origin in ``origins`` — the #4670 redemption binding. Everything
    here except the env-referenced value is inert: safe to commit, log, and show to Agents.
    """

    handle: str = Field(
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
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
    value_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    match_headers: frozenset[str] = Field(
        min_length=1, description="Header names the proxy scans for the placeholder; it passes through anywhere else."
    )
    agent_ids: frozenset[UUID] = Field(min_length=1, description="Agents allowed to redeem this credential.")
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


class EgressDecideConfig(BaseModel):
    """Wiring for ``POST /api/internal/http/decide``: the bearer the colocated egress proxy
    presents, the Agent-bound fence credentials the endpoint resolves, and the egress credential
    registry grants redeem from. ``None`` on ``ConsoleConfigFile`` is the production-safe default
    — the endpoint stays 503 and the proxy fails closed until a deploy deliberately wires it."""

    proxy_token_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    fence_credentials: list[EgressFenceCredentialEntry] = Field(default_factory=list)
    credentials: list[EgressCredentialEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct_env_references(self) -> EgressDecideConfig:
        env_vars = [
            self.proxy_token_env_var,
            *(entry.token_env_var for entry in self.fence_credentials),
            *(entry.value_env_var for entry in self.credentials),
        ]
        if len(set(env_vars)) != len(env_vars):
            raise ValueError("egress decide env var references must be distinct")
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


class LoadedFenceCredential(BaseModel):
    """A fence credential entry after reading its env reference."""

    agent_id: UUID
    token: SecretStr


class LoadedEgressCredential(BaseModel):
    """An egress credential registry entry after reading its env reference."""

    handle: str
    placeholder: str
    value: SecretStr
    match_headers: frozenset[str]
    agent_ids: frozenset[UUID]
    origins: frozenset[HttpOrigin]


class LoadedEgressDecide(BaseModel):
    """The decide endpoint's credentials after reading env references."""

    proxy_token: SecretStr
    fence_credentials: list[LoadedFenceCredential]
    credentials: list[LoadedEgressCredential] = Field(default_factory=list)


def load_egress_decide(config: EgressDecideConfig) -> LoadedEgressDecide:
    """Read the decide endpoint's env-referenced secrets; a missing var fails loud at startup.

    A duplicate credential value would make one secret authenticate two identities — the proxy and
    an Agent, or two Agents — so duplicates are refused like a missing var, without echoing values.
    An egress credential value equal to any configured placeholder would make the "inert"
    placeholder itself the secret, so that is refused the same way.
    """
    proxy_token = os.environ.get(config.proxy_token_env_var)
    if not proxy_token:
        raise RuntimeError(f"missing egress proxy token env var {config.proxy_token_env_var}")
    seen_tokens = {proxy_token}
    loaded: list[LoadedFenceCredential] = []
    for entry in config.fence_credentials:
        token = os.environ.get(entry.token_env_var)
        if not token:
            raise RuntimeError(f"missing fence credential env var {entry.token_env_var} for Agent {entry.agent_id}")
        if token in seen_tokens:
            raise RuntimeError("duplicate egress decide credential values")
        seen_tokens.add(token)
        loaded.append(LoadedFenceCredential(agent_id=entry.agent_id, token=SecretStr(token)))
    placeholders = {entry.placeholder for entry in config.credentials}
    loaded_credentials: list[LoadedEgressCredential] = []
    for credential in config.credentials:
        value = os.environ.get(credential.value_env_var)
        if not value:
            raise RuntimeError(
                f"missing egress credential env var {credential.value_env_var} for handle {credential.handle}"
            )
        if value in seen_tokens:
            raise RuntimeError("duplicate egress decide credential values")
        if value in placeholders:
            raise RuntimeError(f"egress credential {credential.handle} value equals a configured placeholder")
        seen_tokens.add(value)
        loaded_credentials.append(
            LoadedEgressCredential(
                handle=credential.handle,
                placeholder=credential.placeholder,
                value=SecretStr(value),
                match_headers=credential.match_headers,
                agent_ids=credential.agent_ids,
                origins=credential.origins,
            )
        )
    return LoadedEgressDecide(
        proxy_token=SecretStr(proxy_token), fence_credentials=loaded, credentials=loaded_credentials
    )
