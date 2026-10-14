"""Public Action-policy projections and provenance; read-only Kubernetes access."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from agentplane.action_service.policies.registry import Policy
from agentplane.action_service.policies.resources import parse_action_rule
from agentplane.action_service.policy_view import ActionPolicySetView, set_view
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_SETS_PLURAL, PolicySet, RefusedPolicySet, parse_policy_set
from util.kubernetes import CustomObjectsClient

ACTION_POLICY_API = (GROUP, VERSION)
# Preserve these existing ownership stamps when adopting staging resources.
MANAGED_BY_LABEL = f"app.{GROUP}/managed-by"
MANAGED_BY_APP = "integration-app"


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, Any]]


class ActionPolicyReader:
    """Read-only Kubernetes projections, shared by the service and the app."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient) -> None:
        self._namespace = namespace
        self._custom_objects = custom_objects

    async def list_policy_sets(self) -> list[ActionPolicySetView]:
        """Every set in the namespace, refused ones included, in name order."""
        listed = await self._custom_objects.list_namespaced_custom_object(
            *ACTION_POLICY_API, self._namespace, POLICY_SETS_PLURAL
        )
        parsed: list[PolicySet[Policy] | RefusedPolicySet] = [
            parse_policy_set(item, parse_rule=parse_action_rule) for item in _ResourceList.model_validate(listed).items
        ]
        return [set_view(policy_set) for policy_set in sorted(parsed, key=lambda policy_set: policy_set.name)]
