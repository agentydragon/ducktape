"""The constants and the rule parser through which the Action Service reads `PolicySet` and
`PolicyBinding` (`agentplane/policy_sets.py`; the CRDs live in `agentplane/crds`).

A `PolicySet` carries Action policies as `action_auto_approve` rules, which `parse_action_rule` reads;
a rule that does not parse refuses its set (`RefusedPolicySet`), which the informer reports in the
binding's `ActionsReady` condition. A caller ServiceAccount is an ordinary ServiceAccount carrying
`CALLER_LABEL`, which is what admits it here at all -- a Pod running as it and a Connection acting as
it alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue

from agentplane.action_service.policies.kind import Spec
from agentplane.action_service.policies.registry import Policy
from agentplane.crd_group import GROUP

SERVICE_ACCOUNTS_PLURAL = "serviceaccounts"
# Sandbox Controller sets the same label on its Sandboxes' accounts (`ActionCallerLabel` in
# agentplane/sandbox_controller/identity.go).
CALLER_LABEL = f"{GROUP}/use-action-service"
CALLER_LABEL_SELECTOR = f"{CALLER_LABEL}=true"
# On a PolicyBinding, beside the other readers' conditions; written by server-side apply under this
# service's field manager.
ACTIONS_READY_CONDITION = "ActionsReady"
ACTIONS_FIELD_MANAGER = "agentplane-action-service"
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
