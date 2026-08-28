"""Server-level authorization for profile-scoped in-process MCP servers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp_config import AccessProfile
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionCaller
from haku.console.tool_call_actor import AgentActor, RuntimeActor


class InProcessServerAccessPolicy:
    """Deployment-owned, default-deny server grants selected by an Agent access profile."""

    def __init__(self, profiles: tuple[AccessProfile, ...]) -> None:
        self._profile_servers = {profile.id: frozenset(profile.in_process_server_ids) for profile in profiles}

    def allows(self, caller: RuntimeActor | McpExecutionCaller | None, server_id: str) -> bool:
        match caller:
            case (
                AgentActor(access_profile_id=access_profile_id)
                | AgentMcpExecutionCaller(principal=RequestPrincipal(access_profile_id=access_profile_id))
            ):
                return access_profile_id is not None and server_id in self._profile_servers.get(access_profile_id, ())
            case _:
                return False

    def authorizer_for(self, server_id: str) -> Callable[[RuntimeActor, str, dict[str, Any]], str | None]:
        def authorize(actor: RuntimeActor | None, _tool_name: str, _arguments: dict[str, Any]) -> str | None:
            return None if self.allows(actor, server_id) else "in-process server access denied"

        return authorize
