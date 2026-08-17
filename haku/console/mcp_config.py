"""The connected-MCP-server catalog and how to reach each entry.

The console's deploy-time YAML names the MCP servers Haku may drive through the approval
queue; this module models that config, looks entries up by id, and resolves how to reach
each one — the in-process `FastMCP` transport or remote URL, and the static bearer
credential where one applies. The tool-call application service, `McpServerDispatcher`
(`mcp_approval`), and operator OAuth linkage (`mcp_operator_oauth`) build on this shared substrate.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import yaml
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.agents.naming import normalize_agent_name
from haku.console.config import ClaudeRuntimeConfig, HostexecConfig, NodeDaemonsConfig, Settings
from haku.console.provider_connection_registry import ProviderConnectionKind
from mcp_infra.prefix import MCPMountPrefix


class McpServerNotFoundError(LookupError):
    """The configured connected-server catalog has no entry for the requested id."""


class OperatorConnectionProviderDefinition(BaseModel):
    """A deploy-named OAuth application whose secret values come from environment variables."""

    model_config = ConfigDict(extra="forbid")

    kind: ProviderConnectionKind
    client_id_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    client_secret_env_var: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")


class OperatorConnectionDefinition(BaseModel):
    """A deploy-named external-account linkage backed by one configured OAuth application."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1)
    provider: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    scopes: tuple[str, ...] = Field(min_length=1)

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("operator connection display name must not be blank")
        return normalized

    @field_validator("scopes")
    @classmethod
    def _require_distinct_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(scope.strip() for scope in value)
        if any(not scope for scope in normalized):
            raise ValueError("operator connection scopes must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("operator connection scopes must not contain duplicates")
        return normalized


class OperatorConnectionCredential(BaseModel):
    """Inject the acting Operator's configured external-account token during execution."""

    kind: Literal["operator_connection"] = "operator_connection"
    connection: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")


class DynamicOAuthClientRegistration(BaseModel):
    """Register a fresh public OAuth client through RFC 7591 DCR for each connect flow."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["dynamic"] = "dynamic"
    client_name: str = "Haku Console"


class PreregisteredOAuthClient(BaseModel):
    """Use a deploy-provisioned OAuth client and skip Dynamic Client Registration.

    Public PKCE clients use ``client_id``. Confidential clients may instead take their id and
    secret from deploy-injected environment variables, keeping the credential out of the catalog
    ConfigMap and Git history.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["preregistered"] = "preregistered"
    # For an authorization server with no open Dynamic Client Registration (RFC 7591) — e.g.
    # Authentik, which has no /register endpoint, so the server-metadata-declared
    # registration_endpoint is absent and DCR would otherwise 401 against a guessed {server}/register
    # fallback. A pre-registered public/PKCE client_id shared across every OAuth caller of that
    # authorization server, skipping registration entirely. Safe to share: PKCE plus per-request
    # redirect_uri validation secure each caller's auth code exchange independently even though the
    # client_id is the same for all. A public client normally declares this directly; a
    # confidential client can source it from an injected Secret instead.
    client_id: str | None = Field(default=None, min_length=1)
    client_id_env_var: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    client_secret_env_var: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] | None = None

    @model_validator(mode="after")
    def _validate_credential_source(self) -> PreregisteredOAuthClient:
        if (self.client_id is None) == (self.client_id_env_var is None):
            raise ValueError("preregistered OAuth client requires exactly one of client_id or client_id_env_var")
        if (self.client_secret_env_var is None) != (self.token_endpoint_auth_method is None):
            raise ValueError(
                "client_secret_env_var and token_endpoint_auth_method must be configured together for a confidential client"
            )
        return self


type OAuthClientRegistration = Annotated[
    DynamicOAuthClientRegistration | PreregisteredOAuthClient, Field(discriminator="kind")
]


class RemoteServerOAuthAuth(BaseModel):
    """Execute as the acting Operator's account at the remote MCP server's authorization server."""

    kind: Literal["remote_server_oauth"] = "remote_server_oauth"
    client_registration: OAuthClientRegistration
    scopes: list[str] | None = None


class OperatorLoginIdentityCredential(BaseModel):
    """Execute under the acting Operator's own console-login (Authentik) identity: the tool call
    resolves the Operator's stored Authentik login token (captured at login via offline_access),
    which the server exchanges for a per-host token (hostexec)."""

    kind: Literal["operator_login_identity"] = "operator_login_identity"


class StaticBearerAuth(BaseModel):
    """Execute with a fixed, non-operator bearer the console holds — the env-referenced secret
    resolved by `_credential_token`."""

    kind: Literal["static_bearer"] = "static_bearer"
    bearer_token_secret: str


class NoCredential(BaseModel):
    """No backend credential: an in-process server that carries its own (e.g. `haku_routine`, which
    holds the launch-routine secret) or otherwise needs none."""

    kind: Literal["none"] = "none"


# How a server resolves its backend credential for the acting Operator — exactly one variant per
# server. The discriminated union replaces flag+optional fields that could set several at once;
# dispatch by `isinstance` (mypy narrows), never a `kind`-string compare.
type RemoteMcpAuth = Annotated[RemoteServerOAuthAuth | StaticBearerAuth | NoCredential, Field(discriminator="kind")]
type InProcessCredential = Annotated[
    OperatorConnectionCredential | OperatorLoginIdentityCredential | NoCredential, Field(discriminator="kind")
]


class RemoteMcpBackend(BaseModel):
    kind: Literal["remote_mcp"] = "remote_mcp"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: RemoteMcpAuth


class InProcessBackend(BaseModel):
    kind: Literal["in_process"] = "in_process"
    credential: InProcessCredential


type McpBackend = Annotated[RemoteMcpBackend | InProcessBackend, Field(discriminator="kind")]


class McpServerEntry(BaseModel):
    id: str
    backend: McpBackend


class ConsoleMcpConfig(BaseModel):
    servers: list[McpServerEntry] = Field(default_factory=list)


type AutoApprovalPolicyId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]


class AutoApprovalPolicyBase(BaseModel):
    """Fields shared by every node in the deploy-time policy graph."""

    model_config = ConfigDict(extra="forbid")

    id: AutoApprovalPolicyId


class ExactToolsAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Unconditionally auto-approve exact tools on exact configured servers."""

    type: Literal["exact_tools"] = "exact_tools"
    tools: dict[str, set[str]] = Field(min_length=1)

    @field_validator("tools")
    @classmethod
    def _require_named_tools(cls, value: dict[str, set[str]]) -> dict[str, set[str]]:
        for server_id, tools in value.items():
            if not server_id:
                raise ValueError("exact-tools server id must not be blank")
            if not tools:
                raise ValueError(f"exact-tools policy server {server_id!r} must list at least one tool")
            if any(not tool for tool in tools):
                raise ValueError(f"exact-tools policy server {server_id!r} contains a blank tool name")
        return value


class GmailLabelNamespaceAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Conditionally auto-approve Gmail label mutations confined to one namespace."""

    type: Literal["gmail_label_namespace"] = "gmail_label_namespace"
    server: Literal["gmail"] = "gmail"
    label_prefix: str = Field(min_length=1)


class GitHubRepositoryAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Conditionally auto-approve reviewed GitHub reads for one repository."""

    type: Literal["github_repository"] = "github_repository"
    server: Literal["github"] = "github"
    owner: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    tools: set[str] = Field(min_length=1)


class AnyOfAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Auto-approve when any referenced policy auto-approves."""

    type: Literal["any_of"] = "any_of"
    policies: tuple[AutoApprovalPolicyId, ...] = Field(min_length=1)


class NeverAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Never auto-approve; useful as an explicit Agent assignment."""

    type: Literal["never"] = "never"


type AutoApprovalPolicy = Annotated[
    ExactToolsAutoApprovalPolicy
    | GmailLabelNamespaceAutoApprovalPolicy
    | GitHubRepositoryAutoApprovalPolicy
    | AnyOfAutoApprovalPolicy
    | NeverAutoApprovalPolicy,
    Field(discriminator="type"),
]


def _default_auto_approval_policies() -> list[AutoApprovalPolicy]:
    """A fail-closed root for config-less development and schema generation."""
    return [NeverAutoApprovalPolicy(id="manual_review")]


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
    # The root of this Agent's deploy-reviewed auto-approval policy graph. Every static Agent must
    # choose explicitly; dynamically enrolled/OAuth Agents choose during enrollment or reconnect.
    auto_approval_policy: AutoApprovalPolicyId

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
    auto_approval_policy: AutoApprovalPolicyId


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)
    # Non-secret deployment wiring for the optional Console-owned Claude runtime.
    # The real OAuth bearer remains solely in the dedicated iron-proxy.
    claude_runtime: ClaudeRuntimeConfig | None = None
    auto_approval_policies: list[AutoApprovalPolicy] = Field(default_factory=_default_auto_approval_policies)
    operator_connection_providers: dict[str, OperatorConnectionProviderDefinition] = Field(default_factory=dict)
    operator_connections: dict[str, OperatorConnectionDefinition] = Field(default_factory=dict)
    static_agents: list[StaticAgentEntry] = Field(default_factory=list)
    # The `hostexec` in-process server's in-scope machines + token-exchange scope. Non-secret deploy
    # topology, so it lives here beside the `hostexec` catalog entry rather than in an env var. Unset
    # → the server is not offered, no offline_access is requested at operator login, and no operator
    # Authentik token is persisted (nothing would read it).
    hostexec: HostexecConfig | None = None
    node_daemons: NodeDaemonsConfig | None = None

    @property
    def default_agent_auto_approval_policy(self) -> AutoApprovalPolicyId:
        """The unique fail-closed policy offered as the default for new Agent decisions."""
        return next(policy.id for policy in self.auto_approval_policies if isinstance(policy, NeverAutoApprovalPolicy))

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
            if isinstance(server.backend, InProcessBackend):
                credential = server.backend.credential
                if (
                    isinstance(credential, OperatorConnectionCredential)
                    and credential.connection not in self.operator_connections
                ):
                    raise ValueError(
                        f"MCP server {server.id!r} references unknown operator connection {credential.connection!r}"
                    )

        provider_env_vars: set[str] = set()
        for name, provider in self.operator_connection_providers.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid operator connection provider name {name!r}")
            for env_var in (provider.client_id_env_var, provider.client_secret_env_var):
                if env_var in provider_env_vars:
                    raise ValueError(f"duplicate operator connection provider env var {env_var!r}")
                provider_env_vars.add(env_var)

        for name, connection in self.operator_connections.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid operator connection name {name!r}")
            if connection.provider not in self.operator_connection_providers:
                raise ValueError(f"operator connection {name!r} references unknown provider {connection.provider!r}")

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

        policies: dict[str, AutoApprovalPolicy] = {}
        for policy in self.auto_approval_policies:
            if policy.id in policies:
                raise ValueError(f"duplicate auto-approval policy id {policy.id!r}")
            policies[policy.id] = policy
            if isinstance(policy, ExactToolsAutoApprovalPolicy):
                if unknown_servers := set(policy.tools) - server_ids:
                    raise ValueError(
                        f"auto-approval policy {policy.id!r} references unknown MCP servers {sorted(unknown_servers)!r}"
                    )
            elif (
                isinstance(policy, (GmailLabelNamespaceAutoApprovalPolicy, GitHubRepositoryAutoApprovalPolicy))
                and policy.server not in server_ids
            ):
                raise ValueError(f"auto-approval policy {policy.id!r} references unknown MCP server {policy.server!r}")

        for policy in policies.values():
            if isinstance(policy, AnyOfAutoApprovalPolicy):
                unknown_policies = set(policy.policies) - policies.keys()
                if unknown_policies:
                    raise ValueError(
                        f"auto-approval policy {policy.id!r} references unknown policies {sorted(unknown_policies)!r}"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(policy_id: str) -> None:
            if policy_id in visiting:
                raise ValueError(f"auto-approval policy graph contains a cycle at {policy_id!r}")
            if policy_id in visited:
                return
            visiting.add(policy_id)
            policy = policies[policy_id]
            if isinstance(policy, AnyOfAutoApprovalPolicy):
                for member_id in policy.policies:
                    visit(member_id)
            visiting.remove(policy_id)
            visited.add(policy_id)

        for policy_id in policies:
            visit(policy_id)

        fail_closed_policies = [
            policy.id for policy in policies.values() if isinstance(policy, NeverAutoApprovalPolicy)
        ]
        if len(fail_closed_policies) != 1:
            raise ValueError(
                "auto_approval_policies must contain exactly one never policy to use as the fail-closed Agent default"
            )

        for agent in self.static_agents:
            if agent.auto_approval_policy not in policies:
                raise ValueError(
                    f"static Agent {agent.agent_id} references unknown auto-approval policy "
                    f"{agent.auto_approval_policy!r}"
                )
        if self.hostexec is not None:
            if self.node_daemons is None:
                raise ValueError("hostexec requires node_daemons configuration")
            for host, entry in self.hostexec.hosts.items():
                daemon = self.node_daemons.daemons.get(entry.daemon_id)
                if daemon is None:
                    raise ValueError(f"hostexec host {host!r} references unknown daemon {entry.daemon_id!r}")
                if "hostexec" not in daemon.backends:
                    raise ValueError(f"node daemon {entry.daemon_id!r} does not advertise the hostexec backend")
        return self


def server_tool_prefix(server_id: str) -> MCPMountPrefix:
    """Return the configured server's canonical tool namespace."""
    sanitized = re.sub(r"[^a-z0-9]+", "_", server_id.lower()).strip("_")
    return MCPMountPrefix(sanitized)


def load_console_config(settings: Settings) -> ConsoleConfigFile:
    path = settings.config_file
    if path is None or not path.exists():
        return ConsoleConfigFile()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ConsoleConfigFile.model_validate(raw)


def _load_servers(settings: Settings) -> list[McpServerEntry]:
    return load_console_config(settings).mcp.servers


def load_static_agents(settings: Settings) -> list[LoadedStaticAgent]:
    """Read static-agent credentials and controller-fed external user keys from env.

    Raises if a named env var is missing — a misconfigured agent fails loud at startup rather than
    silently accepting no callers. Resolve once (create_app) and reuse; do not read per request."""
    loaded: list[LoadedStaticAgent] = []
    seen_tokens: set[str] = set()
    for entry in load_console_config(settings).static_agents:
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
                auto_approval_policy=entry.auto_approval_policy,
            )
        )
    return loaded


def _server_entry(settings: Settings, server_id: str) -> McpServerEntry:
    for server in _load_servers(settings):
        if server.id == server_id:
            return server
    raise McpServerNotFoundError(f"unknown MCP server: {server_id}")


def _operator_oauth_enabled(server: McpServerEntry) -> bool:
    return isinstance(server.backend, RemoteMcpBackend) and isinstance(server.backend.auth, RemoteServerOAuthAuth)


def _credential_env_name(bearer_token_secret: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", bearer_token_secret).strip("_").upper()
    return f"HAKU_CONSOLE_MCP_CREDENTIAL_{suffix}"


def _credential_token(server_id: str, bearer_token_secret: str) -> str:
    env_name = _credential_env_name(bearer_token_secret)
    token = os.environ.get(env_name)
    if not token:
        raise RuntimeError(f"missing MCP bearer token env var {env_name} for MCP server {server_id}")
    return token


# A server reached over an in-process FastMCP instance instead of a remote URL (see
# McpServerEntry.backend). `fastmcp.client.Client` accepts a `FastMCP` instance
# directly and opens an in-memory `FastMCPTransport` — so both `McpServerDispatcher` and
# `McpServerDispatcher` run the exact same `Client(...)` calls either way; only this
# lookup differs.
#
# The registry holds *builders*, not prebuilt instances: a provider-backed server (gmail,
# google_calendar) is built per execution from the acting Operator's access token, so the
# credential flows in by argument with no shared/ambient state. The token is None only when
# building for tool-schema reflection (`tools/list` never invokes a tool). Credential-free
# servers (routine, tests) use `const_in_process_server`.
InProcessServerBuilder = Callable[[str | None], FastMCP]


class InProcessCredentialKind(StrEnum):
    OPERATOR_CONNECTION = "operator_connection"
    OPERATOR_LOGIN_IDENTITY = "operator_login_identity"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class InProcessServerRegistration:
    builder: InProcessServerBuilder
    credential_kind: InProcessCredentialKind


InProcessServers = dict[str, InProcessServerRegistration]


def const_in_process_server(mcp: FastMCP) -> InProcessServerRegistration:
    """A builder that ignores the token and always returns ``mcp`` (credential-free servers, tests)."""
    return InProcessServerRegistration(builder=lambda _token: mcp, credential_kind=InProcessCredentialKind.NONE)


def validate_in_process_server_bindings(config: ConsoleConfigFile, registrations: InProcessServers) -> None:
    """Reject missing implementations and incompatible in-process credential bindings."""
    for server in config.mcp.servers:
        if not isinstance(server.backend, InProcessBackend):
            continue
        registration = registrations.get(server.id)
        if registration is None:
            raise ValueError(f"MCP server {server.id!r} has no registered in-process implementation")
        configured_kind = InProcessCredentialKind(server.backend.credential.kind)
        if configured_kind is not registration.credential_kind:
            raise ValueError(
                f"MCP server {server.id!r} requires {registration.credential_kind.value!r} credential, "
                f"got {configured_kind.value!r}"
            )


def _transport(
    server: McpServerEntry, in_process: InProcessServers, auth_token: str | None
) -> tuple[FastMCP | StreamableHttpTransport | str, str | None]:
    """Resolve the MCP transport and any transport-level authentication.

    In-process builders consume the backend credential while constructing their server, so the
    resulting in-memory transport must not receive it again as client authentication. Remote HTTP
    transports instead carry the credential as their bearer authentication.
    """
    match server.backend:
        case InProcessBackend():
            registration = in_process.get(server.id)
            if registration is None:
                raise RuntimeError(f"MCP server {server.id!r} has no in-process registration")
            return registration.builder(auth_token), None
        case RemoteMcpBackend(url=url, headers=headers):
            return (
                (StreamableHttpTransport(url, headers=headers, auth=auth_token), None) if headers else (url, auth_token)
            )
