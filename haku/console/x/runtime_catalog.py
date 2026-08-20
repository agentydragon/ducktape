"""Application composition for the runtime implementations linked into Console."""

from __future__ import annotations

from haku.console.config import ClaudeRuntimeConfig
from haku.console.x.claude_code.client import cli_over_websocket
from haku.console.x.claude_code.runtime import ClaudeRuntimeAdapter
from haku.console.x.runtime import RuntimeClientFactory, RuntimeRegistry, RuntimeResources
from haku.console.x.sandbox_claims import SandboxClaims
from haku.console.x.system_prompt import SystemPromptTemplate


def projection_registry() -> RuntimeRegistry:
    """All linked provider interpreters, without execution credentials or sandbox resources."""
    adapter = ClaudeRuntimeAdapter()
    return RuntimeRegistry({adapter.kind: adapter})


def claude_registry(
    config: ClaudeRuntimeConfig,
    claims: SandboxClaims,
    *,
    system_prompt: SystemPromptTemplate,
    client_factory: RuntimeClientFactory = cli_over_websocket,
) -> RuntimeRegistry:
    """Compose the currently supported production execution catalog: Claude only."""
    adapter = ClaudeRuntimeAdapter(client_factory=client_factory)
    return RuntimeRegistry(
        {adapter.kind: adapter},
        {
            adapter.kind: RuntimeResources(
                claims=claims,
                session_ttl_seconds=config.session_ttl_seconds,
                cwd=config.cwd,
                environment=config.claude_environment(),
                mcp_server_urls={"haku-console": config.mcp_url},
                system_prompt=system_prompt,
            )
        },
    )
