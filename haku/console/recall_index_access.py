"""Server-side Recall authorization derived from durable Agent access profiles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp.execution import AgentMcpExecutionCaller, McpExecutionCaller, OperatorMcpExecutionCaller
from haku.console.mcp_config import AccessProfile
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor


class RecallIndexAccessPolicy:
    """Deployment-owned logical-index grants for Agents and the authenticated Operator."""

    def __init__(self, profiles: tuple[AccessProfile, ...], *, configured_index_ids: Iterable[str]) -> None:
        self._profile_indexes = {profile.id: frozenset(profile.recall_index_ids) for profile in profiles}
        # The browser Operator is trusted to inspect the whole reviewed Recall catalog. Keep this
        # separate from Agent grants: a configured index may intentionally be withheld from every
        # Agent profile while remaining visible to the Operator.
        self._operator_indexes = tuple(sorted(set(configured_index_ids)))

    def allowed_indexes(self, caller: RuntimeActor | McpExecutionCaller | None) -> tuple[str, ...]:
        match caller:
            case OperatorActor() | OperatorMcpExecutionCaller():
                return self._operator_indexes
            case (
                AgentActor(access_profile_id=access_profile_id)
                | AgentMcpExecutionCaller(principal=RequestPrincipal(access_profile_id=access_profile_id))
            ):
                pass
            case _:
                return ()
        if access_profile_id is None:
            return ()
        return tuple(sorted(self._profile_indexes.get(access_profile_id, ())))

    def allows(self, caller: RuntimeActor | McpExecutionCaller | None, index_id: str) -> bool:
        return index_id in self.allowed_indexes(caller)

    def authorize_index_tool(self, actor: RuntimeActor, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Reject an unauthorized index request before it reaches the approval queue."""
        if tool_name == "search":
            index_id = arguments.get("index_id")
            return None if isinstance(index_id, str) and self.allows(actor, index_id) else "recall index access denied"
        if tool_name == "index_status":
            return None if self.allowed_indexes(actor) else "recall index access denied"
        return None
