"""Backend-neutral Console harness catalog.

The sandbox and runner lifecycle is Haku infrastructure.  A harness registration pairs that generic
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
class HarnessLaunchSpec:
    """Generic facts a harness launch builder translates into its native argv/configuration."""

    cwd: str
    environment: Mapping[str, str]
    mcp_servers: Mapping[str, HarnessMcpServer]
    appended_system_prompt: str | None
    resume_from: int | None


@dataclass(frozen=True, slots=True)
class HarnessMcpServer:
    """One explicitly configured MCP capability available to a native harness."""

    url: str
    bearer_environment_variable: str


@dataclass(frozen=True, slots=True)
class HarnessKey:
    """The immutable Agent/harness pair selected for one conversation.

    ``HarnessKind`` is deliberately only a protocol discriminator.  Execution resources are
    selected with this key so two Agents using the same protocol cannot accidentally share a
    sandbox pool, prompt, environment, or MCP endpoint.
    """

    agent_id: UUID
    harness_kind: HarnessKind


class HarnessAdapter(Protocol):
    """Provider-owned launch behavior behind one immutable ``HarnessKind``.

    Launch is all a harness owes the neutral-operation generation (#4667): the runner interprets
    its own native stream and projects it, so the Console composes no native protocol and projects
    no native frames for any harness.
    """

    @property
    def kind(self) -> HarnessKind: ...

    @property
    def display_name(self) -> str: ...

    def build_launch(self, launch: HarnessLaunchSpec) -> HarnessLaunch:
        """The native `HarnessLaunch` the journal bridge (#4667) sends the runner directly."""
        ...


@dataclass(frozen=True, slots=True)
class AgentHarnessResources:
    """Agent-owned execution resources for one explicitly supported native harness."""

    claims: SandboxClaims
    session_ttl_seconds: int
    cwd: str
    environment: Mapping[str, str]
    # Authentication is minted per session and added only when the authenticated runner connects.
    # The endpoint and prompt still belong to this Agent launch target, not to the protocol adapter.
    mcp_server_urls: Mapping[str, str]
    system_prompt: SystemPromptTemplate
    agent_id: UUID
    access_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfiguredHarness:
    """One provider adapter paired with one Agent's execution resources."""

    adapter: HarnessAdapter
    resources: AgentHarnessResources


class UnsupportedHarnessError(LookupError):
    """No adapter was registered for a conversation's immutable harness kind."""


class HarnessNotConfiguredError(RuntimeError):
    """A known harness has no sandbox/credential configuration on this replica."""


class HarnessRegistry:
    """Immutable harness definitions plus the subset configured for execution.

    Read paths need only adapters.  A launch-capable Console registers resources separately for the
    same keys; absence is a registry fact rather than a half-initialized provider adapter.
    """

    def __init__(
        self,
        adapters: Mapping[HarnessKind, HarnessAdapter],
        resources: Mapping[HarnessKey, AgentHarnessResources] | None = None,
    ):
        self._adapters = dict(adapters)
        self._resources_by_identity: dict[HarnessKey, AgentHarnessResources] = {}
        for key, source in (resources or {}).items():
            identity = key
            if source.agent_id != identity.agent_id:
                raise ValueError("harness resource Agent disagrees with its registry key")
            self._resources_by_identity[identity] = source
        for kind, adapter in self._adapters.items():
            if adapter.kind is not kind:
                raise ValueError(f"harness adapter key {kind!r} disagrees with adapter kind {adapter.kind!r}")
        unknown_resources = {identity.harness_kind for identity in self._resources_by_identity} - self._adapters.keys()
        if unknown_resources:
            raise ValueError(f"harness resources have no adapter: {sorted(kind.value for kind in unknown_resources)}")

    def adapter(self, kind: HarnessKind) -> HarnessAdapter:
        try:
            return self._adapters[kind]
        except KeyError as error:
            raise UnsupportedHarnessError(f"harness kind {kind!r} is not registered") from error

    def configured(
        self, identity: HarnessKey, *, access_profile_id: str | None = None, expected_profile_id: str | None = None
    ) -> ConfiguredHarness:
        """Return launch resources, validating the pinned Agent/profile when supplied.

        Harness-owned execution paths must pass the session's immutable Agent/harness identity;
        there is no kind-only lookup or default harness.
        """
        if expected_profile_id is not None:
            if access_profile_id is not None and access_profile_id != expected_profile_id:
                raise TypeError("access profile was supplied twice")
            access_profile_id = expected_profile_id
        resource = self.resources_for(identity)
        if access_profile_id != resource.access_profile_id and (
            access_profile_id is not None or resource.access_profile_id is not None
        ):
            raise HarnessNotConfiguredError("pinned access profile does not match harness resources")

        adapter = self.adapter(identity.harness_kind)
        return ConfiguredHarness(adapter=adapter, resources=resource)

    def resources_for(self, identity: HarnessKey) -> AgentHarnessResources:
        """Return the resources for an explicit Agent/harness key without profile validation."""
        self.adapter(identity.harness_kind)
        try:
            return self._resources_by_identity[identity]
        except KeyError as error:
            raise HarnessNotConfiguredError(
                f"harness {identity.harness_kind!r} is not configured for Agent {identity.agent_id}"
            ) from error

    def configured_for(self, identity: HarnessKey, *, access_profile_id: str | None = None) -> ConfiguredHarness:
        """Explicit spelling for execution callers selecting by immutable session identity."""
        return self.configured(identity, access_profile_id=access_profile_id)

    def __getitem__(self, kind: HarnessKind) -> HarnessAdapter:
        return self.adapter(kind)

    def __contains__(self, kind: HarnessKind) -> bool:
        return kind in self._adapters

    @property
    def kinds(self) -> frozenset[HarnessKind]:
        return frozenset(self._adapters)

    @property
    def configured_kinds(self) -> frozenset[HarnessKind]:
        return frozenset(identity.harness_kind for identity in self._resources_by_identity)

    @property
    def configured_identities(self) -> frozenset[HarnessKey]:
        return frozenset(self._resources_by_identity)

    async def aclose(self) -> None:
        """Close each configured claims client once, even if registrations share one."""
        closed: set[int] = set()
        for resources in self._resources_by_identity.values():
            identity = id(resources.claims)
            if identity in closed:
                continue
            closed.add(identity)
            await resources.claims.aclose()
