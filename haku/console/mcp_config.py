"""The connected-MCP-server catalog and how to reach each entry.

The console's deploy-time YAML names the MCP servers Haku may drive through the approval
queue; this module models that config, looks entries up by id, and resolves how to reach
each one — the in-process `FastMCP` transport or remote URL, and the static bearer
credential where one applies. The tool-call application service, reflection adapter
(`mcp_approval`), and operator OAuth linkage (`mcp_operator_oauth`) build on this shared substrate.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from uuid import UUID

import yaml
from fastmcp import FastMCP
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from haku.console.agents.naming import normalize_agent_name
from haku.console.config import Settings
from haku.console.provider_connection_registry import ProviderConnectionKind
from mcp_infra.prefix import MCPMountPrefix


class McpServerNotFoundError(LookupError):
    """The configured connected-server catalog has no entry for the requested id."""


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
    # For an in-process server that executes as the acting Operator's linked external account
    # (Google today): the tool call resolves that Operator's provider access token before
    # executing. It is the console's own replacement for the Airlock-brokered token.
    provider_connection: ProviderConnectionKind | None = None
    # For an in-process server that executes under the acting Operator's own identity (hostexec): the
    # tool call resolves the Operator's stored Authentik login token (captured via offline_access)
    # before executing, which the server exchanges for a per-host token. See ServerAuthMode.
    operator_identity_token: bool = False

    @model_validator(mode="after")
    def _reject_conflicting_operator_token_sources(self) -> McpServerEntry:
        # Each names the operator-linked token source; setting more than one is meaningless because
        # exactly one resolves the credential. A static bearer_token_secret coexisting is
        # intentional (reflection/fallback wiring).
        sources = [
            name
            for name, set_ in (
                ("provider_connection", self.provider_connection is not None),
                ("operator_oauth", self.operator_oauth is not None),
                ("operator_identity_token", self.operator_identity_token),
            )
            if set_
        ]
        if len(sources) > 1:
            raise ValueError(
                f"MCP server {self.id!r} sets multiple operator token sources ({', '.join(sources)}); "
                "an operator-linked server uses exactly one"
            )
        return self


class ConsoleMcpConfig(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


class StaticAgentEntry(BaseModel):
    """Controller-owned identity and secret reference for one static Agent slot.

    The UUID is the durable Agent identity. The display name is presentation only, globally reserved
    under Haku's compatibility-caseless normalization. Secret and owner values remain env references.
    """

    agent_id: UUID
    display_name: str
    token_env_var: str
    # External deploy contract retained for safe image/config rollout: the value is Authentik's
    # stable OIDC `sub`/user_id seed. It is resolved to an Operator UUID once at startup and is
    # never live request authority.
    operator_subject_env: str

    @field_validator("display_name")
    @classmethod
    def _require_normalized_display_name(cls, value: str) -> str:
        normalized = normalize_agent_name(value)
        if normalized.display_name != value:
            raise ValueError(f"static Agent display_name must be normalized as {normalized.display_name!r}")
        return value


class LoadedStaticAgent(BaseModel):
    """A static agent after reading env references, but before canonical Operator resolution."""

    agent_id: UUID
    display_name: str
    secret_reference: str
    token: SecretStr
    operator_external_user_key: str


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)
    static_agents: list[StaticAgentEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_unique_identity(self) -> ConsoleConfigFile:
        server_ids: set[str] = set()
        server_prefixes: set[MCPMountPrefix] = set()
        for server in self.mcp.servers:
            prefix = server_tool_prefix(server.id)
            if server.id in server_ids:
                raise ValueError(f"duplicate MCP server id {server.id!r}")
            if prefix in server_prefixes:
                raise ValueError(f"duplicate MCP server tool prefix {prefix!r}")
            server_ids.add(server.id)
            server_prefixes.add(prefix)

        agent_ids: set[UUID] = set()
        name_keys: set[str] = set()
        for agent in self.static_agents:
            name_key = normalize_agent_name(agent.display_name).reservation_key
            if agent.agent_id in agent_ids:
                raise ValueError(f"duplicate static Agent id {agent.agent_id}")
            if name_key in name_keys:
                raise ValueError(f"duplicate normalized static Agent display name {agent.display_name!r}")
            agent_ids.add(agent.agent_id)
            name_keys.add(name_key)
        return self


def server_tool_prefix(server_id: str) -> MCPMountPrefix:
    """Return the configured server's canonical tool namespace."""
    sanitized = re.sub(r"[^a-z0-9]+", "_", server_id.lower()).strip("_")
    return MCPMountPrefix(sanitized)


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
    seen_tokens: set[str] = set()
    for entry in _load_config(settings).static_agents:
        token = os.environ.get(entry.token_env_var)
        if not token:
            raise RuntimeError(f"missing token env var {entry.token_env_var} for Agent {entry.agent_id}")
        if token in seen_tokens:
            raise RuntimeError("duplicate static agent bearer tokens")
        seen_tokens.add(token)
        external_user_key = os.environ.get(entry.operator_subject_env)
        if not external_user_key:
            raise RuntimeError(
                f"missing operator external-user-key env var {entry.operator_subject_env} for Agent {entry.agent_id}"
            )
        loaded.append(
            LoadedStaticAgent(
                agent_id=entry.agent_id,
                display_name=entry.display_name,
                secret_reference=entry.token_env_var,
                token=SecretStr(token),
                operator_external_user_key=external_user_key,
            )
        )
    return loaded


def _server_entry(settings: Settings, server_id: str) -> McpServerEntry:
    for server in _load_servers(settings):
        if server.id == server_id:
            return server
    raise McpServerNotFoundError(f"unknown MCP server: {server_id}")


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
#
# The registry holds *builders*, not prebuilt instances: a provider-backed server (gmail,
# google_calendar) is built per execution from the acting Operator's access token, so the
# credential flows in by argument with no shared/ambient state. The token is None only when
# building for tool-schema reflection (`tools/list` never invokes a tool). Credential-free
# servers (routine, tests) use `const_in_process_server`.
InProcessServerBuilder = Callable[[str | None], FastMCP]
InProcessServers = dict[str, InProcessServerBuilder]


def const_in_process_server(mcp: FastMCP) -> InProcessServerBuilder:
    """A builder that ignores the token and always returns ``mcp`` (credential-free servers, tests)."""
    return lambda _token: mcp


def _transport(server: McpServerEntry, in_process: InProcessServers, auth_token: str | None) -> FastMCP | str:
    if builder := in_process.get(server.id):
        return builder(auth_token)
    if server.server_url is not None:
        return server.server_url
    raise RuntimeError(f"MCP server {server.id!r} has no server_url and no in-process registration")
