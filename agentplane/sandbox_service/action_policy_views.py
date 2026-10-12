"""Public Action-policy projections and provenance; read-only Kubernetes access."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from agentplane.action_service.policies.resources import (
    POLICY_SETS_PLURAL,
    ActionPolicySet,
    InvalidResource,
    parse_policy_set,
)
from agentplane.action_service.policy_view import ActionPolicySetView, set_view
from agentplane.crd_group import GROUP, VERSION
from agentplane.sandbox_service.errors import InventoryError
from util.kubernetes import CustomObjectsClient

ACTION_POLICY_API = (GROUP, VERSION)
# Preserve these existing ownership stamps when adopting staging resources.
MANAGED_BY_LABEL = f"app.{GROUP}/managed-by"
MANAGED_BY_APP = "integration-app"


class UnknownPolicySetError(InventoryError):
    """A binding naming a set the namespace does not hold, which would grant nothing.

    The CRD admits any string in `spec.policySets` and the Action Service treats a name that
    resolves to nothing as contributing nothing, so a dangling name is a state the system already
    handles. This refuses one at the moment it would be written; one the operator deletes
    afterwards still lands there without granting anything from the missing set.
    """

    def __init__(self, names: list[str], *, kind: str) -> None:
        super().__init__(f"no {kind} in the namespace is named {', '.join(names)}")
        self.names = names


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
        return [set_view(policy_set) for name, policy_set in sorted((await self._policy_sets_by_name()).items())]

    async def _policy_sets_by_name(self) -> dict[str, ActionPolicySet | InvalidResource]:
        listed = await self._custom_objects.list_namespaced_custom_object(
            *ACTION_POLICY_API, self._namespace, POLICY_SETS_PLURAL
        )
        return {
            parsed.metadata.name: parsed for parsed in map(parse_policy_set, _ResourceList.model_validate(listed).items)
        }
