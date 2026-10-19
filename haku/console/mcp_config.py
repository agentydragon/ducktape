"""Deploy-time configuration for Console MCP backends and auto-approval policies.

The deploy-time YAML names configured backends. This module models that config, looks entries up
by id, and resolves each one to its registered `FastMCP` builder. The Console MCP protocol and
catalog refresh are retired; its dispatcher remains for the approval ledger, with no production
execution backend currently registered.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from haku.console.identity.naming import normalize_agent_name
from haku.console.tool_call_actor import RuntimeActor


class McpServerNotFoundError(LookupError):
    """The configured connected-server catalog has no entry for the requested id."""


class NoCredential(BaseModel):
    """The in-process backend does not need a credential supplied by the dispatcher."""

    kind: Literal["none"] = "none"


# How a server resolves its backend credential for the acting Operator — exactly one variant per
# server. The discriminated union replaces flag+optional fields that could set several at once;
# dispatch by `isinstance` (mypy narrows), never a `kind`-string compare.
type InProcessCredential = Annotated[NoCredential, Field(discriminator="kind")]


class InProcessBackend(BaseModel):
    kind: Literal["in_process"] = "in_process"
    credential: InProcessCredential


class McpServerEntry(BaseModel):
    id: str
    backend: InProcessBackend


class ConsoleMcpConfig(BaseModel):
    servers: dict[str, McpServerEntry] = Field(default_factory=dict)


type AutoApprovalPolicyId = Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")]
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


class RetiredGrantSelfListAutoApprovalPolicy(AutoApprovalPolicyBase):
    """Read the last deployed grants policy during a rolling ConfigMap update.

    The retired backend is never registered and this policy always evaluates as manual. Keeping
    the old shape parseable lets new replicas start if they see the previous ConfigMap briefly.
    """

    type: Literal["grant_self_list"] = "grant_self_list"
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
    | RetiredGrantSelfListAutoApprovalPolicy
    | AnyOfAutoApprovalPolicy
    | NeverAutoApprovalPolicy,
    Field(discriminator="type"),
]


class AccessProfile(BaseModel):
    """A deploy-reviewed capability bundle assigned to one durable Agent.

    The profile deliberately gathers all durable Agent authority in one config catalog. Credential
    bindings authenticate an Agent; they never independently select either capability.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    auto_approval_policy: AutoApprovalPolicyId
    # Explicit access to credential-free in-process servers whose data is held by Console itself.
    # This is independent from auto-approval (whether a call skips review).
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
    auto_approval_policies: list[AutoApprovalPolicy] = Field(min_length=1)
    access_profiles: list[AccessProfile] = Field(min_length=1)
    default_access_profile_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    static_agents: dict[str, StaticAgentEntry] = Field(default_factory=dict)

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
        server_ids: set[str] = set()
        server_prefixes: set[str] = set()
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
            if isinstance(policy, ExactToolsAutoApprovalPolicy) and (unknown_servers := set(policy.tools) - server_ids):
                raise ValueError(
                    f"auto-approval policy {policy.id!r} references unknown MCP servers {sorted(unknown_servers)!r}"
                )

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
        configured_in_process_servers = {server.id for server in self.mcp.servers.values()}
        for profile in profiles.values():
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


def server_tool_prefix(server_id: str) -> str:
    """Return the configured server's canonical tool namespace."""
    sanitized = re.sub(r"[^a-z0-9]+", "_", server_id.lower()).strip("_")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", sanitized):
        raise ValueError(f"invalid MCP server tool prefix derived from id {server_id!r}: {sanitized!r}")
    return sanitized


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
# The registry holds *builders*, not prebuilt instances, so a future credentialed server can build
# per execution from the acting Operator's resolved token with no shared/ambient state.
# Credential-free servers can use a constant builder that ignores the resolved token.
InProcessServerBuilder = Callable[[str | None], FastMCP]
InProcessRequestAuthorizer = Callable[[RuntimeActor, str, dict[str, Any]], str | None]


class InProcessCredentialKind(StrEnum):
    NONE = "none"


@dataclass(frozen=True, slots=True)
class InProcessServerRegistration:
    builder: InProcessServerBuilder
    credential_kind: InProcessCredentialKind
    authorizer: InProcessRequestAuthorizer | None = None


InProcessServers = dict[str, InProcessServerRegistration]


def validate_in_process_server_bindings(config: ConsoleConfigFile, registrations: InProcessServers) -> None:
    """Reject missing implementations and incompatible in-process credential bindings.

    The previously deployed `grants` backend is an exception: newer replicas accept that legacy
    ConfigMap entry while rolling to the ConfigMap that removes it, but never register a backend.
    """
    for server in config.mcp.servers.values():
        registration = registrations.get(server.id)
        if registration is None:
            if server.id == "grants":
                continue
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
