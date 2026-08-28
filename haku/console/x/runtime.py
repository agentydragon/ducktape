"""Backend-neutral Console runtime catalog.

The sandbox and runner lifecycle is Haku infrastructure.  A runtime registration pairs that generic
infrastructure with the one harness adapter that knows how to launch and speak a provider's native
protocol.  The runner itself remains one Pydantic-envelope process bridge for every harness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from haku.console.harnesses.kind import HarnessKind
from haku.console.session.sandbox_claims import SandboxClaims
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.runner.protocol import HarnessLaunch


@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    """Generic facts a harness launch builder translates into its native argv/configuration."""

    cwd: str
    environment: Mapping[str, str]
    mcp_servers: Mapping[str, RuntimeMcpServer]
    appended_system_prompt: str | None
    resume_from: int | None


@dataclass(frozen=True, slots=True)
class RuntimeMcpServer:
    """One explicitly configured MCP capability available to a native harness."""

    url: str
    bearer_environment_variable: str


@dataclass(frozen=True, slots=True)
class RuntimeKey:
    """The immutable Agent/runtime pair selected for one conversation.

    ``HarnessKind`` is deliberately only a protocol discriminator.  Execution resources are
    selected with this key so two Agents using the same protocol cannot accidentally share a
    sandbox pool, prompt, environment, or MCP endpoint.
    """

    agent_id: UUID
    runtime_kind: HarnessKind


class RuntimeAdapter(Protocol):
    """Provider-owned launch behavior behind one immutable ``HarnessKind``.

    Launch is all a runtime owes the neutral-operation generation (#4667): the runner interprets
    its own native stream and projects it, so the Console composes no native protocol and projects
    no native frames for any harness.
    """

    @property
    def kind(self) -> HarnessKind: ...

    @property
    def display_name(self) -> str: ...

    def build_launch(self, launch: RuntimeLaunch) -> HarnessLaunch:
        """The native `HarnessLaunch` the journal bridge (#4667) sends the runner directly."""
        ...


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """Agent-owned execution resources for one explicitly supported native runtime."""

    claims: SandboxClaims
    session_ttl_seconds: int
    cwd: str
    environment: Mapping[str, str]
    # Authentication is minted per session and added only when the authenticated runner connects.
    # The endpoint and prompt still belong to this Agent launch target, not to the protocol adapter.
    mcp_server_urls: Mapping[str, str]
    system_prompt: SystemPromptTemplate
    # Optional on the compatibility path.  New launch-capable registrations always set both and
    # are addressed through ``RuntimeKey``; old tests/replicas may still provide kind-only rows.
    agent_id: UUID | None = None
    access_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfiguredRuntime:
    """One provider adapter paired with one Agent's execution resources."""

    adapter: RuntimeAdapter
    resources: AgentRuntimeResources


class UnsupportedRuntimeError(LookupError):
    """No adapter was registered for a conversation's immutable runtime kind."""


class RuntimeNotConfiguredError(RuntimeError):
    """A known runtime has no sandbox/credential configuration on this replica."""


class RuntimeRegistry:
    """Immutable runtime definitions plus the subset configured for execution.

    Read paths need only adapters.  A launch-capable Console registers resources separately for the
    same keys; absence is a registry fact rather than a half-initialized provider adapter.
    """

    def __init__(
        self,
        adapters: Mapping[HarnessKind, RuntimeAdapter],
        resources: Mapping[HarnessKind | RuntimeKey, AgentRuntimeResources] | None = None,
    ):
        self._adapters = dict(adapters)
        self._resources: dict[HarnessKind, AgentRuntimeResources] = {}
        self._resources_by_identity: dict[RuntimeKey, AgentRuntimeResources] = {}
        for key, source in (resources or {}).items():
            if isinstance(key, HarnessKind):
                self._resources[key] = source
                continue
            identity = key
            original_resource = source
            if original_resource.agent_id is not None and original_resource.agent_id != identity.agent_id:
                raise ValueError("runtime resource Agent disagrees with its registry key")
            if original_resource.agent_id is None:
                selected = AgentRuntimeResources(
                    claims=original_resource.claims,
                    session_ttl_seconds=original_resource.session_ttl_seconds,
                    cwd=original_resource.cwd,
                    environment=original_resource.environment,
                    mcp_server_urls=original_resource.mcp_server_urls,
                    system_prompt=original_resource.system_prompt,
                    agent_id=identity.agent_id,
                    access_profile_id=original_resource.access_profile_id,
                )
            else:
                selected = original_resource
            self._resources_by_identity[identity] = selected
        for kind, adapter in self._adapters.items():
            if adapter.kind is not kind:
                raise ValueError(f"runtime adapter key {kind!r} disagrees with adapter kind {adapter.kind!r}")
        unknown_resources = (
            self._resources.keys() | {identity.runtime_kind for identity in self._resources_by_identity}
        ) - self._adapters.keys()
        if unknown_resources:
            raise ValueError(f"runtime resources have no adapter: {sorted(kind.value for kind in unknown_resources)}")

    def adapter(self, kind: HarnessKind) -> RuntimeAdapter:
        try:
            return self._adapters[kind]
        except KeyError as error:
            raise UnsupportedRuntimeError(f"runtime kind {kind!r} is not registered") from error

    def configured(
        self,
        kind_or_agent: HarnessKind | UUID | RuntimeKey | None = None,
        runtime_kind: HarnessKind | None = None,
        *,
        access_profile_id: str | None = None,
        agent_id: UUID | None = None,
        expected_profile_id: str | None = None,
    ) -> ConfiguredRuntime:
        """Return launch resources, validating the pinned Agent/profile when supplied.

        The one-argument form is retained for rolling compatibility and projection-focused tests.
        Runtime-owned execution paths should pass ``agent_id`` and ``runtime_kind`` from the
        session's immutable identity; no kind-only fallback is attempted for a pinned identity.
        """
        if expected_profile_id is not None:
            if access_profile_id is not None and access_profile_id != expected_profile_id:
                raise TypeError("access profile was supplied twice")
            access_profile_id = expected_profile_id
        if agent_id is not None:
            if kind_or_agent is not None:
                raise TypeError("Agent id was supplied twice")
            kind_or_agent = agent_id
        if isinstance(kind_or_agent, RuntimeKey):
            if runtime_kind is not None:
                raise TypeError("runtime_kind is only valid once with a RuntimeKey")
            runtime_kind = kind_or_agent.runtime_kind
            kind_or_agent = kind_or_agent.agent_id
        if isinstance(kind_or_agent, HarnessKind):
            if runtime_kind is not None:
                raise TypeError("runtime_kind is only valid with an Agent id")
            kind = kind_or_agent
            resource = self._resources.get(kind)
        else:
            if kind_or_agent is None:
                raise TypeError("runtime kind or Agent id is required")
            if runtime_kind is None:
                raise TypeError("runtime_kind is required with an Agent id")
            kind = runtime_kind
            resource = self._resources_by_identity.get(RuntimeKey(kind_or_agent, kind))
            if (
                resource is not None
                and resource.access_profile_id is not None
                and access_profile_id != resource.access_profile_id
            ):
                raise RuntimeNotConfiguredError("pinned access profile does not match runtime resources")
        adapter = self.adapter(kind)
        try:
            resources = resource
            if resources is None:
                raise KeyError(kind)
        except KeyError as error:
            raise RuntimeNotConfiguredError(f"runtime kind {kind!r} is not configured for execution") from error
        return ConfiguredRuntime(adapter=adapter, resources=resources)

    def configured_for(self, identity: RuntimeKey, *, access_profile_id: str | None = None) -> ConfiguredRuntime:
        """Explicit spelling for execution callers selecting by immutable session identity."""
        return self.configured(identity.agent_id, identity.runtime_kind, access_profile_id=access_profile_id)

    def __getitem__(self, kind: HarnessKind) -> RuntimeAdapter:
        return self.adapter(kind)

    def __contains__(self, kind: HarnessKind) -> bool:
        return kind in self._adapters

    @property
    def kinds(self) -> frozenset[HarnessKind]:
        return frozenset(self._adapters)

    @property
    def configured_kinds(self) -> frozenset[HarnessKind]:
        return frozenset(self._resources) | frozenset(identity.runtime_kind for identity in self._resources_by_identity)

    @property
    def configured_identities(self) -> frozenset[RuntimeKey]:
        return frozenset(self._resources_by_identity)

    async def aclose(self) -> None:
        """Close each configured claims client once, even if registrations share one."""
        closed: set[int] = set()
        for resources in (*self._resources.values(), *self._resources_by_identity.values()):
            identity = id(resources.claims)
            if identity in closed:
                continue
            closed.add(identity)
            await resources.claims.aclose()
