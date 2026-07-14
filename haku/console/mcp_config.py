"""The connected-MCP-server catalog and how to reach each entry.

The console's deploy-time YAML names the MCP servers Haku may drive through the approval
queue; this module models that config, looks entries up by id, and resolves how to reach
each one — the in-process `FastMCP` transport or remote URL, and the static bearer
credential where one applies. Both the approval router (`mcp_approval`) and the operator
OAuth linkage (`mcp_operator_oauth`) build on this shared substrate.
"""

from __future__ import annotations

import os
import re
from uuid import UUID

import yaml
from fastapi import HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel, Field, SecretStr

from haku.console.config import Settings
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

STATIC_AGENT_CLIENT_ID_PREFIX = "static-agent:"


def static_agent_client_id(agent: str) -> str:
    """Namespace FastMCP's static client ids away from OAuth DCR client ids."""
    return f"{STATIC_AGENT_CLIENT_ID_PREFIX}{agent}"


def static_agent_name_from_client_id(client_id: str) -> str | None:
    if not client_id.startswith(STATIC_AGENT_CLIENT_ID_PREFIX):
        return None
    return client_id.removeprefix(STATIC_AGENT_CLIENT_ID_PREFIX)


class McpOperatorOAuthConfig(BaseModel):
    enabled: bool = True
    client_name: str = "Haku Console"
    scopes: list[str] | None = None
    # For an authorization server with no open Dynamic Client Registration (RFC 7591) —
    # e.g. Authentik, which has no /register endpoint, so the server-metadata-declared
    # registration_endpoint is absent and DCR would otherwise 401 against a guessed
    # {server}/register fallback. A pre-registered public/PKCE client_id shared across
    # every OAuth caller of that authorization server, skipping registration entirely.
    # Safe to share: PKCE plus per-request redirect_uri validation secure each caller's
    # auth code exchange independently even though the client_id is the same for all.
    static_client_id: str | None = None


class McpServerEntry(BaseModel):
    id: str
    # None for a server reached via an in-process FastMCP instance instead of a remote
    # URL (see McpToolExecutor/McpMetadataProvider's `in_process_servers` registry,
    # e.g. haku.console.tools.gmail's `gmail` server) — resolved at runtime by id,
    # not by anything in this config model.
    server_url: str | None = None
    bearer_token_secret: str | None = None
    operator_oauth: McpOperatorOAuthConfig | None = None


class ConsoleMcpConfig(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


class StaticAgentEntry(BaseModel):
    """A static machine agent: a fixed bearer token bound to one operator.

    `agent` is the agent's stable identity — the `/mcp` static bearer's `client_id` and the audit
    `caller_principal`. The bearer and stable external user key are named env vars, not literals: the
    value lives in the deployment Secret, never in this YAML. The external key is resolved through
    the configured identity trust domain at startup and never carried on live request paths.
    """

    agent: str
    token_env_var: str
    # External deploy contract retained for safe image/config rollout: the value is Authentik's
    # stable OIDC `sub`/user_id seed. It is resolved to an Operator UUID once at startup and is
    # never live request authority.
    operator_subject_env: str


class LoadedStaticAgent(BaseModel):
    """A static agent after reading env references, but before canonical Operator resolution."""

    agent: str
    token: SecretStr
    operator_external_user_key: str


class ResolvedStaticAgent(BaseModel):
    """A static agent whose configured owner has been resolved to a canonical Operator."""

    agent: str
    token: SecretStr
    operator_id: UUID


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)
    static_agents: list[StaticAgentEntry] = Field(default_factory=list)


def _load_config(settings: Settings) -> ConsoleConfigFile:
    path = settings.config_file
    if path is None or not path.exists():
        return ConsoleConfigFile()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ConsoleConfigFile.model_validate(raw)


def _load_servers(settings: Settings) -> list[McpServerEntry]:
    return _load_config(settings).mcp.servers


def load_static_agents(settings: Settings) -> list[LoadedStaticAgent]:
    """Read static-agent credentials and controller-fed external user keys from env.

    Raises if a named env var is missing — a misconfigured agent fails loud at startup rather than
    silently accepting no callers. Resolve once (create_app) and reuse; do not read per request."""
    loaded: list[LoadedStaticAgent] = []
    seen_agents: set[str] = set()
    seen_tokens: set[str] = set()
    for entry in _load_config(settings).static_agents:
        if entry.agent in seen_agents:
            raise RuntimeError(f"duplicate static agent id {entry.agent!r}")
        seen_agents.add(entry.agent)
        token = os.environ.get(entry.token_env_var)
        if not token:
            raise RuntimeError(f"missing token env var {entry.token_env_var} for agent {entry.agent!r}")
        if token in seen_tokens:
            raise RuntimeError("duplicate static agent bearer tokens")
        seen_tokens.add(token)
        external_user_key = os.environ.get(entry.operator_subject_env)
        if not external_user_key:
            raise RuntimeError(
                f"missing operator external-user-key env var {entry.operator_subject_env} for agent {entry.agent!r}"
            )
        loaded.append(
            LoadedStaticAgent(agent=entry.agent, token=SecretStr(token), operator_external_user_key=external_user_key)
        )
    return loaded


def resolve_static_agents(
    loaded_agents: list[LoadedStaticAgent], identity_store: PostgresOperatorIdentityStore
) -> list[ResolvedStaticAgent]:
    """Resolve every configured static-agent owner to a canonical active Operator."""
    return [
        ResolvedStaticAgent(
            agent=agent.agent,
            token=agent.token,
            operator_id=identity_store.resolve_configured_external_user_key(agent.operator_external_user_key),
        )
        for agent in loaded_agents
    ]


def _server_entry(settings: Settings, server_id: str) -> McpServerEntry:
    for server in _load_servers(settings):
        if server.id == server_id:
            return server
    raise HTTPException(status_code=404, detail=f"unknown MCP server: {server_id}")


def _operator_oauth_enabled(server: McpServerEntry) -> bool:
    return bool(server.operator_oauth and server.operator_oauth.enabled)


def _credential_env_name(bearer_token_secret: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", bearer_token_secret).strip("_").upper()
    return f"HAKU_CONSOLE_MCP_CREDENTIAL_{suffix}"


def _credential_token(server: McpServerEntry) -> str | None:
    if server.bearer_token_secret is None:
        return None
    env_name = _credential_env_name(server.bearer_token_secret)
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeError(f"missing MCP bearer token env var {env_name} for MCP server {server.id}")
    return token


# A server reached over an in-process FastMCP instance instead of a remote URL (see
# McpServerEntry.server_url). `fastmcp.client.Client` accepts a `FastMCP` instance
# directly and opens an in-memory `FastMCPTransport` — so both `McpToolExecutor` and
# `McpMetadataProvider` run the exact same `Client(...)` calls either way; only this
# lookup differs.
InProcessServers = dict[str, FastMCP]


def _transport(server: McpServerEntry, in_process: InProcessServers) -> FastMCP | str:
    if in_process_server := in_process.get(server.id):
        return in_process_server
    if server.server_url is not None:
        return server.server_url
    raise RuntimeError(f"MCP server {server.id!r} has no server_url and no in-process registration")
