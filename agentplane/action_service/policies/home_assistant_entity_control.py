"""`home_assistant_entity_control`: a Home Assistant service call matches when its arguments can reach
only one configured entity, with a service configured for that entity.

Every Home Assistant write is the one generic `ha_call_service` tool, so listing it under
`exact_actions` would grant every service on every entity, `lock.unlock` and `homeassistant.restart`
included. This kind constrains the arguments instead, as an allow-list: an argument this policy has
not reviewed, or one that could widen the target, is a mismatch. A mismatch sends the request to the
human path; it is never a deny.
"""

from __future__ import annotations

from collections.abc import Set
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from agentplane.action_service.catalog import Key
from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind

# The upstream Home Assistant MCP server's tool name.
_SERVICE_CALL_ACTION = "ha_call_service"

# Home Assistant's identifier alphabet. A call's `entity_id` is matched against the configured ids by
# exact string, so an id outside it, such as a comma-separated list, can never be configured as the
# one entity a call is approved for.
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]


class HomeAssistantEntityControl(Kind):
    type: Literal[PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL]
    entities: dict[EntityId, Annotated[Set[ServiceName], Field(min_length=1)]] = Field(
        min_length=1,
        description="Home Assistant entity ids by the services a call may use on each; nothing else is matched.",
    )

    @field_validator("actions")
    @classmethod
    def _only_the_service_call_action(cls, value: dict[Key, frozenset[Key]]) -> dict[Key, frozenset[Key]]:
        for group, names in value.items():
            if unreviewed := names - {_SERVICE_CALL_ACTION}:
                raise ValueError(f"group {group!r} lists {sorted(unreviewed)}; only {_SERVICE_CALL_ACTION} is reviewed")
        return value
