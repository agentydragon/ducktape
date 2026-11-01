"""Action Service constants and validation for `action_auto_approve` PolicySet rules.

`parse_action_rule` validates one rule during cdk8s synthesis. The Go runtime reads the PolicySets and
reports `ActionsReady` on bindings. A caller ServiceAccount is an ordinary ServiceAccount carrying
`CALLER_LABEL`, which admits it here -- a Pod running as it and a Connection acting as it alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue

from agentplane.action_service.policies.kind import Spec
from agentplane.action_service.policies.registry import Policy
from agentplane.crd_group import GROUP

# Sandbox Controller sets the same label on its Sandboxes' accounts (`ActionCallerLabel` in
# agentplane/sandbox_controller/identity.go).
CALLER_LABEL = f"{GROUP}/use-action-service"
# On a PolicyBinding, beside the other readers' conditions.
ACTIONS_READY_CONDITION = "ActionsReady"
ACTION_RULE_TYPE = "action_auto_approve"


class AutoApproveRule(Spec):
    """A PolicySet rule this service reads: a request `policy` matches is auto-approved."""

    type: Literal["action_auto_approve"]
    policy: Policy


def parse_action_rule(raw: Mapping[str, JsonValue]) -> Policy | None:
    """A PolicySet rule's auto-approve policy, or None for another reader's rule type."""
    if raw["type"] != ACTION_RULE_TYPE:
        return None
    return AutoApproveRule.model_validate(raw).policy
