"""Home Assistant service-call auto-approval: confined to named entities and services.

Every Home Assistant write goes through one generic tool, ``ha_call_service``, so a name-matching
policy cannot express "this lamp and nothing else" — listing the tool would grant every service on
every entity, including ``lock.unlock``, ``alarm_control_panel.disarm`` and ``homeassistant.restart``.
This evaluator is the argument-scoped alternative: the configured map fixes which entity may be
targeted and which services it may be targeted with, and everything else stays operator-gated.

The call is admitted only when the arguments cannot reach anything else, so the checks are an
allow-list rather than a deny-list — an argument key this policy has not reviewed sends the call to
manual approval instead of being ignored.
"""

from __future__ import annotations

import logging
from typing import Any

from haku.console.auto_approval.decision import AutoApprovalDecision, AutoApproved, NotAutoApproved

logger = logging.getLogger(__name__)

CALL_SERVICE_TOOL = "ha_call_service"

# Argument keys that cannot widen what the call touches: the target itself is checked separately,
# and the rest only shape the response. `ws_command` is deliberately absent — it is a raw
# websocket escape hatch that bypasses the domain/service/entity triple entirely.
_REVIEWED_ARGUMENTS = frozenset(
    {
        "domain",
        "service",
        "entity_id",
        "data",
        "return_response",
        "wait",
        "verbose",
        "result_fields",
        "result_attribute_keys",
    }
)

# Keys that redirect a service call at something other than `entity_id`. Home Assistant resolves
# any of these to a target set, so one inside `data` would silently widen an otherwise-scoped call.
_RETARGETING_KEYS = frozenset({"entity_id", "target", "area_id", "device_id", "label_id"})


def evaluate_entity_control(
    tool_name: str, arguments: dict[str, Any], entities: dict[str, set[str]]
) -> AutoApprovalDecision:
    """Evaluate one ``ha_call_service`` call against the configured entity/service allow-list."""
    try:
        if tool_name != CALL_SERVICE_TOOL:
            return NotAutoApproved(f"{tool_name!r} is not the reviewed service-call tool")

        if unreviewed := sorted(set(arguments) - _REVIEWED_ARGUMENTS):
            return NotAutoApproved(f"call carries argument(s) this policy has not reviewed: {', '.join(unreviewed)}")

        entity_id = arguments.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            # A missing or list-valued target is how a call reaches every entity in a domain.
            return NotAutoApproved("auto-approval requires exactly one entity_id, given as a string")
        if entity_id not in entities:
            return NotAutoApproved(f"{entity_id} is not an entity this policy controls")

        domain, service = arguments.get("domain"), arguments.get("service")
        if domain != entity_id.split(".", 1)[0]:
            return NotAutoApproved(f"domain {domain!r} does not match the entity's own domain")
        if service not in entities[entity_id]:
            return NotAutoApproved(f"service {service!r} is not allowed on {entity_id}")

        data = arguments.get("data") or {}
        if not isinstance(data, dict):
            return NotAutoApproved("data must be an object")
        if retargeting := sorted(set(data) & _RETARGETING_KEYS):
            return NotAutoApproved(f"data would retarget the call via: {', '.join(retargeting)}")

        return AutoApproved(f"{domain}.{service} targets only {entity_id}, which this policy controls")
    except Exception:
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
        return NotAutoApproved("Home Assistant auto-approval evaluation failed")
