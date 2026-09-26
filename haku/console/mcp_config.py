"""The MCP-server catalog and how to reach each entry.

The console's deploy-time YAML names the MCP servers Haku may drive through the approval
queue; this module models that config, looks entries up by id, and resolves each one to its
registered in-process `FastMCP` builder. The tool-call application service and
`McpServerDispatcher` (`approval`) build on this shared substrate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.config import KubernetesAuthorizationConfig
from haku.console.identity.naming import normalize_agent_name
from haku.console.oauth.provider_connection_registry import ProviderConnectionKind
from haku.console.tool_call_actor import RuntimeActor
from haku.recall_index.config import ConfiguredRecallIndex, GitRecallIndexDefinition
from haku.sandbox.config import SandboxEnvironmentConfig
from mcp_infra.prefix import MCPMountPrefix


class McpServerNotFoundError(LookupError):
    """The configured connected-server catalog has no entry for the requested id."""


class OperatorConnectionProviderDefinition(BaseModel):
    """A deploy-named OAuth application, optionally provisioned by nested settings."""

    model_config = ConfigDict(extra="forbid")

    kind: ProviderConnectionKind
    client_id: str | None = Field(default=None, min_length=1)
    client_secret: SecretStr | None = None

    @model_validator(mode="after")
    def _complete_credentials(self) -> OperatorConnectionProviderDefinition:
        if (self.client_id is None) != (self.client_secret is None):
            raise ValueError("operator connection provider credentials require both client_id and client_secret")
        return self


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


class OperatorLoginIdentityCredential(BaseModel):
    """Execute under the acting Operator's own console-login (Authentik) identity: the tool call
    resolves the Operator's stored Authentik login token (captured at login via offline_access),
    which the server exchanges for a per-host token (hostexec)."""

    kind: Literal["operator_login_identity"] = "operator_login_identity"


class NoCredential(BaseModel):
    """No backend credential: an in-process server that carries its own (e.g. `haku_routine`, which
    holds the launch-routine secret) or otherwise needs none."""

    kind: Literal["none"] = "none"


# How a server resolves its backend credential for the acting Operator — exactly one variant per
# server. The discriminated union replaces flag+optional fields that could set several at once;
# dispatch by `isinstance` (mypy narrows), never a `kind`-string compare.
type InProcessCredential = Annotated[
    OperatorConnectionCredential | OperatorLoginIdentityCredential | NoCredential, Field(discriminator="kind")
]


class InProcessBackend(BaseModel):
    kind: Literal["in_process"] = "in_process"
    credential: InProcessCredential


class McpServerEntry(BaseModel):
    id: str
    backend: InProcessBackend


class ConsoleMcpConfig(BaseModel):
    servers: dict[str, McpServerEntry] = Field(default_factory=dict)


type AutoApprovalPolicyId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]
type RecallIndexId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")]
type InProcessServerId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]


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


class HomeAssistantEntityControlAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Conditionally auto-approve Home Assistant service calls against named entities.

    Every Home Assistant write goes through the single generic ``ha_call_service`` tool, so an
    ``exact_tools`` entry cannot express "this lamp and nothing else" — it would grant every service
    on every entity. This kind constrains the arguments instead: ``entities`` maps an entity id to
    the services it may be called with, and the evaluator
    (``haku/console/auto_approval/home_assistant.py``) rejects anything that could retarget the call.
    """

    type: Literal["home_assistant_entity_control"] = "home_assistant_entity_control"
    server: str = Field(min_length=1)
    entities: dict[str, set[str]] = Field(min_length=1)

    @field_validator("entities")
    @classmethod
    def _require_domain_qualified_entities(cls, value: dict[str, set[str]]) -> dict[str, set[str]]:
        for entity_id, services in value.items():
            # The evaluator derives the expected `domain` argument from this prefix, so an entity id
            # without one would make every domain compare unequal and silently never auto-approve.
            if entity_id.count(".") != 1 or not all(entity_id.split(".")):
                raise ValueError(f"entity id {entity_id!r} must be domain-qualified, as in 'light.desk'")
            if not services:
                raise ValueError(f"entity {entity_id!r} must list at least one service")
            if any(not service or "." in service for service in services):
                raise ValueError(f"entity {entity_id!r} lists a blank or domain-qualified service name")
        return value


class GrantSelfListAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Conditionally auto-approve an Agent listing its OWN grants (`list_grants(principal='self')`).

    Only the explicit own-scope read auto-approves; omitting `principal` or naming a principal stays
    manual. The omitted form lists every declared grant, the named-principal form returns authority
    declared for that exact subject, and `self` resolves the caller's trusted request principal.
    ``include_inactive`` does not widen that principal scope.
    """

    type: Literal["grant_self_list"] = "grant_self_list"
    server: str = Field(min_length=1)


class KubernetesPassthroughAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Conditionally auto-deny passthrough calls when covered by direct agent Kubernetes grants/SAR."""

    type: Literal["kubernetes_passthrough"] = "kubernetes_passthrough"
    server: str = Field(min_length=1)


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
    | HomeAssistantEntityControlAutoApprovalPolicy
    | GrantSelfListAutoApprovalPolicy
    | KubernetesPassthroughAutoApprovalPolicy
    | AnyOfAutoApprovalPolicy
    | NeverAutoApprovalPolicy,
    Field(discriminator="type"),
]


class AccessProfile(BaseModel):
    """A deploy-reviewed capability bundle assigned to one durable Agent.

    The profile deliberately gathers all durable Agent authority in one config catalog: its
    current auto-approval policy and the logical Recall indexes it may later search. Credential
    bindings authenticate an Agent; they never independently select either capability.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    auto_approval_policy: AutoApprovalPolicyId
    recall_index_ids: set[RecallIndexId] = Field(default_factory=set)
    # Explicit access to credential-free in-process servers whose data is held by Console itself.
    # This is independent from auto-approval (whether a call skips review) and Recall access
    # (whether a particular index can be searched).
    in_process_server_ids: set[InProcessServerId] = Field(default_factory=set)


class StaticAgentEntry(BaseModel):
    """Controller-owned identity and credentials for one static Agent slot.

    The UUID is the durable Agent identity. The display name is presentation only, globally reserved
    under Haku's compatibility-caseless normalization. Pydantic injects the secret and owner
    values directly into the slot selected by the surrounding mapping key.
    """

    agent_id: UUID
    display_name: str
    token: SecretStr
    # Authentik's stable OIDC `sub`/user_id seed. It is resolved to an Operator UUID once at
    # startup and is never live request authority.
    operator_subject: str = Field(min_length=1)
    # Static Agents choose an explicit capability profile. OAuth/DCR Agents select one in the
    # browser enrollment decision alongside their display name.
    access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")

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
    access_profile_id: str


class ConsoleConfigFile(BaseModel):
    mcp: ConsoleMcpConfig = Field(default_factory=ConsoleMcpConfig)
    # libgit2 does not inherit Python/OpenSSL environment variables. Configure its process-wide
    # trust store explicitly before any HTTPS recall source is cloned or fetched.
    git_ca_bundle: Path = Path("/etc/ssl/certs/ca-certificates.crt")
    auto_approval_policies: list[AutoApprovalPolicy] = Field(min_length=1)
    access_profiles: list[AccessProfile] = Field(min_length=1)
    default_access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    operator_connection_providers: dict[str, OperatorConnectionProviderDefinition] = Field(default_factory=dict)
    operator_connections: dict[str, OperatorConnectionDefinition] = Field(default_factory=dict)
    static_agents: dict[str, StaticAgentEntry] = Field(default_factory=dict)
    # Declared source configuration, not a harness convention. This is intentionally in the
    # deploy-owned non-secret catalog: adding a new source is a reviewed Git change, and matching
    # credentials remain environment references on that entry.
    recall_indexes: dict[str, ConfiguredRecallIndex] = Field(default_factory=dict)
    # Standing Kubernetes policy is selected by the same deploy-managed access profile that owns
    # the Agent's other durable authority. Unset keeps the internal proxy endpoint fail-closed.
    kubernetes_authorization: KubernetesAuthorizationConfig | None = None
    # A deploy-reviewed fail-safe maximum for approval-created temporary grants. Tool schema bounds
    # remain useful client guidance, but these server-side settings are authoritative.
    kubernetes_grant_max_lifetime_seconds: int = Field(default=3600, ge=1, le=86_400)
    http_grant_max_lifetime_seconds: int = Field(default=3600, ge=1, le=86_400)
    # The one Agent Sandbox environment the `sandbox` in-process server hands out: which warm pool
    # it claims from and the reviewed bootstrap each claim runs. Unset → the server is not offered.
    # Editing this keeps live claims usable; a claim whose recorded pod properties no longer match
    # is reported in its `warnings` rather than refused (see haku/sandbox/README.md).
    agent_sandbox: SandboxEnvironmentConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_runtime_shape(cls, value: object) -> object:
        # This model intentionally ignores the independent top-level `settings` section in the
        # shared YAML, so it cannot globally forbid extras. Reject retired siblings explicitly
        # rather than silently accepting stale deployment wiring.
        if isinstance(value, dict):
            if "claude_runtime" in value:
                raise ValueError("claude_runtime was replaced by harnesses.claude_code")
            if "chat_runtimes" in value:
                raise ValueError("chat_runtimes was renamed to harnesses")
            if "default_chat_agent_id" in value:
                raise ValueError("default_chat_agent_id was renamed to default_agent_id")
            if "harnesses" in value:
                raise ValueError("harnesses (hosted-agent sessions) was removed")
            if "matrix_launch" in value:
                raise ValueError("matrix_launch (the Matrix channel) was removed")
            if "launchable_agents" in value:
                raise ValueError("launchable_agents (hosted-agent sessions) was removed")
        return value

    @model_validator(mode="after")
    def _require_unique_identity(self) -> ConsoleConfigFile:
        index_ids: set[str] = set()
        mirror_paths: set[str] = set()
        for slot, index in self.recall_indexes.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
                raise ValueError(f"invalid recall index slot {slot!r}")
            if index.index_id in index_ids:
                raise ValueError(f"duplicate recall index id {index.index_id!r}")
            index_ids.add(index.index_id)
            if isinstance(index, GitRecallIndexDefinition):
                mirror_path = str(index.mirror_path)
                if mirror_path in mirror_paths:
                    raise ValueError(f"multiple Git recall indexes share mirror path {mirror_path!r}")
                mirror_paths.add(mirror_path)
        server_ids: set[str] = set()
        server_prefixes: set[MCPMountPrefix] = set()
        for slot, server in self.mcp.servers.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
                raise ValueError(f"invalid MCP server slot {slot!r}")
            prefix = server_tool_prefix(server.id)
            if server.id in server_ids:
                raise ValueError(f"duplicate MCP server id {server.id!r}")
            if prefix in server_prefixes:
                raise ValueError(f"duplicate MCP server tool prefix {prefix!r}")
            server_ids.add(server.id)
            server_prefixes.add(prefix)
            credential = server.backend.credential
            if (
                isinstance(credential, OperatorConnectionCredential)
                and credential.connection not in self.operator_connections
            ):
                raise ValueError(
                    f"MCP server {server.id!r} references unknown operator connection {credential.connection!r}"
                )

        for name in self.operator_connection_providers:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid operator connection provider name {name!r}")

        for name, connection in self.operator_connections.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"invalid operator connection name {name!r}")
            if connection.provider not in self.operator_connection_providers:
                raise ValueError(f"operator connection {name!r} references unknown provider {connection.provider!r}")

        agent_ids: set[UUID] = set()
        name_keys: set[str] = set()
        for slot, agent in self.static_agents.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slot):
                raise ValueError(f"invalid static Agent slot {slot!r}")
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
            elif isinstance(policy, GmailLabelNamespaceAutoApprovalPolicy) and policy.server not in server_ids:
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

        profiles: dict[str, AccessProfile] = {}
        for profile in self.access_profiles:
            if profile.id in profiles:
                raise ValueError(f"duplicate access profile id {profile.id!r}")
            if profile.auto_approval_policy not in policies:
                raise ValueError(
                    f"access profile {profile.id!r} references unknown auto-approval policy "
                    f"{profile.auto_approval_policy!r}"
                )
            profiles[profile.id] = profile
        if self.default_access_profile_id not in profiles:
            raise ValueError(f"default access profile {self.default_access_profile_id!r} is not configured")
        if self.kubernetes_authorization is not None:
            unknown_kubernetes_profiles = (
                set(self.kubernetes_authorization.subjects_by_access_profile) - profiles.keys()
            )
            if unknown_kubernetes_profiles:
                raise ValueError(
                    "Kubernetes authorization references unknown access profiles "
                    f"{sorted(unknown_kubernetes_profiles)!r}"
                )

        configured_recall_indexes = {index.index_id for index in self.recall_indexes.values()}
        configured_in_process_servers = {server.id for server in self.mcp.servers.values()}
        if "kubernetes" in configured_in_process_servers and self.kubernetes_authorization is None:
            raise ValueError("the Kubernetes in-process server requires Kubernetes authorization configuration")
        for profile in profiles.values():
            unknown_recall_indexes = set(profile.recall_index_ids) - configured_recall_indexes
            if unknown_recall_indexes:
                raise ValueError(
                    f"access profile {profile.id!r} references unknown Recall indexes "
                    f"{sorted(unknown_recall_indexes)!r}"
                )
            if unknown_in_process_servers := set(profile.in_process_server_ids) - configured_in_process_servers:
                raise ValueError(
                    f"access profile {profile.id!r} references unknown in-process MCP servers "
                    f"{sorted(unknown_in_process_servers)!r}"
                )

        for agent in self.static_agents.values():
            if agent.access_profile_id not in profiles:
                raise ValueError(
                    f"static Agent {agent.agent_id} references unknown access profile {agent.access_profile_id!r}"
                )
        return self


def server_tool_prefix(server_id: str) -> MCPMountPrefix:
    """Return the configured server's canonical tool namespace."""
    sanitized = re.sub(r"[^a-z0-9]+", "_", server_id.lower()).strip("_")
    return MCPMountPrefix(sanitized)


def _load_servers(config: ConsoleConfigFile) -> list[McpServerEntry]:
    return list(config.mcp.servers.values())


def load_static_agents(config: ConsoleConfigFile) -> list[LoadedStaticAgent]:
    """Validate static-agent credentials once before canonical Operator resolution."""
    loaded: list[LoadedStaticAgent] = []
    seen_tokens: set[str] = set()
    for slot, entry in config.static_agents.items():
        token = entry.token.get_secret_value()
        if token in seen_tokens:
            raise RuntimeError("duplicate static agent bearer tokens")
        seen_tokens.add(token)
        loaded.append(
            LoadedStaticAgent(
                agent_id=entry.agent_id,
                display_name=entry.display_name,
                secret_reference=slot,
                token=entry.token,
                operator_external_user_key=entry.operator_subject,
                access_profile_id=entry.access_profile_id,
            )
        )
    return loaded


def _server_entry(config: ConsoleConfigFile, server_id: str) -> McpServerEntry:
    for server in _load_servers(config):
        if server.id == server_id:
            return server
    raise McpServerNotFoundError(f"unknown MCP server: {server_id}")


# `fastmcp.client.Client` accepts a `FastMCP` instance directly and opens an in-memory
# `FastMCPTransport`, so a dispatcher drives a registered server through ordinary MCP calls.
#
# The registry holds *builders*, not prebuilt instances: a provider-backed server (gmail,
# google_calendar) is built per execution from the acting Operator's access token, so the
# credential flows in by argument with no shared/ambient state. The token is None only when
# building for tool-schema reflection (`tools/list` never invokes a tool). Credential-free
# servers (routine, tests) use `const_in_process_server`.
InProcessServerBuilder = Callable[[str | None], FastMCP]
InProcessRequestAuthorizer = Callable[[RuntimeActor, str, dict[str, Any]], str | None]


class InProcessCredentialKind(StrEnum):
    OPERATOR_CONNECTION = "operator_connection"
    OPERATOR_LOGIN_IDENTITY = "operator_login_identity"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class InProcessServerRegistration:
    builder: InProcessServerBuilder
    credential_kind: InProcessCredentialKind
    authorizer: InProcessRequestAuthorizer | None = None


InProcessServers = dict[str, InProcessServerRegistration]


def const_in_process_server(mcp: FastMCP) -> InProcessServerRegistration:
    """A builder that ignores the token and always returns ``mcp`` (credential-free servers, tests)."""
    return InProcessServerRegistration(builder=lambda _token: mcp, credential_kind=InProcessCredentialKind.NONE)


def validate_in_process_server_bindings(config: ConsoleConfigFile, registrations: InProcessServers) -> None:
    """Reject missing implementations and incompatible in-process credential bindings."""
    for server in config.mcp.servers.values():
        registration = registrations.get(server.id)
        if registration is None:
            raise ValueError(f"MCP server {server.id!r} has no registered in-process implementation")
        configured_kind = InProcessCredentialKind(server.backend.credential.kind)
        if configured_kind is not registration.credential_kind:
            raise ValueError(
                f"MCP server {server.id!r} requires {registration.credential_kind.value!r} credential, "
                f"got {configured_kind.value!r}"
            )


def _in_process_server(server: McpServerEntry, in_process: InProcessServers, auth_token: str | None) -> FastMCP:
    """Build the server's registered in-process instance, consuming its backend credential."""
    registration = in_process.get(server.id)
    if registration is None:
        raise RuntimeError(f"MCP server {server.id!r} has no in-process registration")
    return registration.builder(auth_token)
