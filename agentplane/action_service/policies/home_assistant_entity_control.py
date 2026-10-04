"""`home_assistant_entity_control`: a Home Assistant service call matches when its arguments can reach
only one configured entity, with a service configured for that entity.

Every Home Assistant write is the one generic `ha_call_service` tool, so listing it under
`exact_actions` would grant every service on every entity, `lock.unlock` and `homeassistant.restart`
included. This kind constrains the arguments instead, as an allow-list: an argument this policy has
not reviewed, or one that could widen the target, is a mismatch. A mismatch sends the request to the
human path; it is never a deny.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, field_validator

from agentplane.action_service.catalog import Key
from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind, Matched, NotMatched

# The upstream Home Assistant MCP server's tool name.
_SERVICE_CALL_ACTION = "ha_call_service"

# Home Assistant's identifier alphabet. A call's `entity_id` is matched against the configured ids by
# exact string, so an id outside it, such as a comma-separated list, can never be configured as the
# one entity a call is approved for.
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]

# Argument keys that cannot widen what the call touches: the target itself is checked separately,
# and the rest only shape the response. `ws_command` is deliberately absent: it is a raw websocket
# escape hatch that bypasses the domain/service/entity triple entirely.
_REVIEWED_ARGUMENTS = frozenset(
    {
        # keep-sorted start
        "data",
        "domain",
        "entity_id",
        "result_attribute_keys",
        "result_fields",
        "return_response",
        "service",
        "verbose",
        "wait",
        # keep-sorted end
    }
)

# Keys that redirect a service call at something other than `entity_id`. Home Assistant resolves
# any of these to a target set, so one inside `data` would silently widen an otherwise-scoped call.
_RETARGETING_KEYS = frozenset({"entity_id", "target", "area_id", "device_id", "label_id"})


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


def evaluate(policy: HomeAssistantEntityControl, arguments: Mapping[str, JsonValue]) -> Matched | NotMatched:
    """The registry has already checked that the Action is listed, which `actions` limits to the one
    reviewed tool."""
    if unreviewed := sorted(arguments.keys() - _REVIEWED_ARGUMENTS):
        return NotMatched(f"call carries argument(s) this policy has not reviewed: {', '.join(unreviewed)}")

    entity_id = arguments.get("entity_id")
    if not isinstance(entity_id, str):
        # A missing or list-valued target is how a call reaches every entity in a domain.
        return NotMatched("auto-approval requires exactly one entity_id, given as a string")
    if (services := policy.entities.get(entity_id)) is None:
        return NotMatched(f"{entity_id} is not an entity this policy controls")

    domain, service = arguments.get("domain"), arguments.get("service")
    if domain != entity_id.partition(".")[0]:
        return NotMatched(f"domain {domain!r} does not match the entity's own domain")
    if not isinstance(service, str) or service not in services:
        return NotMatched(f"service {service!r} is not allowed on {entity_id}")

    data = arguments.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return NotMatched("data must be an object")
    if retargeting := sorted(data.keys() & _RETARGETING_KEYS):
        return NotMatched(f"data would retarget the call via: {', '.join(retargeting)}")

    return Matched(f"{domain}.{service} targets only {entity_id}, which this policy controls")
