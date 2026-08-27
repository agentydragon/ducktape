"""Deploy-time credentials of the internal HTTP egress decide endpoint (#4670).

The ``egress_decide`` section of the console config file declares which bearer the colocated
egress proxy presents and which Agent-bound fence credentials the endpoint resolves; secret
values stay in env-var references like every other console-only bearer. ``load_egress_decide``
reads the references once at startup and hands ``HttpDecideService`` the resolved values.
"""

from __future__ import annotations

import os
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator


class EgressFenceCredentialEntry(BaseModel):
    """One Agent-bound fence credential the internal HTTP egress decide endpoint accepts.

    Endpoint-scoped by construction: the referenced secret is resolved only by the decide service,
    never registered with the general Agent bearer authority, so it is invalid for MCP, session,
    and operator APIs. Deploy config binds each env-referenced secret to its Agent until minted
    per-claim fence credentials (#4670's fence-credential work item) replace this static wiring.
    """

    agent_id: UUID
    token_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")


class EgressDecideConfig(BaseModel):
    """Wiring for ``POST /api/internal/http/decide``: the bearer the colocated egress proxy
    presents plus the Agent-bound fence credentials the endpoint resolves. ``None`` on
    ``ConsoleConfigFile`` is the production-safe default — the endpoint stays 503 and the
    proxy fails closed until a deploy deliberately wires it."""

    proxy_token_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    fence_credentials: list[EgressFenceCredentialEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct_env_references(self) -> EgressDecideConfig:
        env_vars = [self.proxy_token_env_var, *(entry.token_env_var for entry in self.fence_credentials)]
        if len(set(env_vars)) != len(env_vars):
            raise ValueError("egress decide env var references must be distinct")
        return self


class LoadedFenceCredential(BaseModel):
    """A fence credential entry after reading its env reference."""

    agent_id: UUID
    token: SecretStr


class LoadedEgressDecide(BaseModel):
    """The decide endpoint's credentials after reading env references."""

    proxy_token: SecretStr
    fence_credentials: list[LoadedFenceCredential]


def load_egress_decide(config: EgressDecideConfig) -> LoadedEgressDecide:
    """Read the decide endpoint's env-referenced secrets; a missing var fails loud at startup.

    A duplicate credential value would make one secret authenticate two identities — the proxy and
    an Agent, or two Agents — so duplicates are refused like a missing var, without echoing values.
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
    return LoadedEgressDecide(proxy_token=SecretStr(proxy_token), fence_credentials=loaded)
