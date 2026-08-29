"""Application composition for the runtime implementations linked into Console."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from haku.console.config import ClaudeCodeImplementationConfig, RuntimeRegistrationConfig
from haku.console.session.sandbox_claims import SandboxClaims
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.x.claude_code.runtime import ClaudeRuntimeAdapter
from haku.console.x.codex_app_server.config import CodexAppServerImplementationConfig
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter
from haku.console.x.runtime import AgentRuntimeResources, HarnessKey, RuntimeAdapter, RuntimeRegistry
from haku.runner.codex.options import CodexModelProvider


def projection_registry() -> RuntimeRegistry:
    """All linked provider interpreters, without execution credentials or sandbox resources."""
    adapters = (ClaudeRuntimeAdapter(), CodexRuntimeAdapter())
    return RuntimeRegistry({adapter.kind: adapter for adapter in adapters})


@dataclass(frozen=True, slots=True)
class RuntimeRegistration:
    """One adapter plus the deploy-owned resources that make it launchable."""

    adapter: RuntimeAdapter
    resources: AgentRuntimeResources

    @property
    def key(self) -> HarnessKey:
        """The explicit Agent/harness resource selector."""
        return HarnessKey(self.resources.agent_id, self.adapter.kind)


def execution_registry(*registrations: RuntimeRegistration) -> RuntimeRegistry:
    """Compose every runtime this replica is deliberately configured to execute."""
    adapters = {registration.adapter.kind: registration.adapter for registration in registrations}
    resources = {registration.key: registration.resources for registration in registrations}
    if len(resources) != len(registrations):
        raise ValueError("duplicate configured Agent/harness resource")
    return RuntimeRegistry(adapters, resources)


def runtime_registration(
    config: RuntimeRegistrationConfig,
    claims: SandboxClaims,
    *,
    system_prompt: SystemPromptTemplate,
    access_profile_id: str | None = None,
    execution_environment: Mapping[str, str] | None = None,
) -> RuntimeRegistration:
    """Build one runtime from shared resources and its discriminated implementation."""
    implementation = config.implementation
    if isinstance(implementation, ClaudeCodeImplementationConfig):
        adapter: RuntimeAdapter = ClaudeRuntimeAdapter()
    elif isinstance(implementation, CodexAppServerImplementationConfig):
        adapter = CodexRuntimeAdapter(
            model=implementation.model,
            reasoning_effort=implementation.reasoning_effort,
            model_provider=CodexModelProvider(
                provider_id=implementation.provider_id,
                name=implementation.provider_name,
                base_url=implementation.api_base_url,
                api_key_env_var=implementation.api_key_env_var,
            ),
        )
    else:
        raise AssertionError(f"unhandled runtime implementation: {type(implementation).__name__}")
    return RuntimeRegistration(
        adapter=adapter,
        resources=AgentRuntimeResources(
            claims=claims,
            session_ttl_seconds=config.session_ttl_seconds,
            cwd=config.cwd,
            environment={**config.environment(), **(execution_environment or {})},
            mcp_server_urls={"haku-console": config.mcp_url},
            system_prompt=system_prompt,
            agent_id=config.agent_id,
            access_profile_id=access_profile_id,
        ),
    )
