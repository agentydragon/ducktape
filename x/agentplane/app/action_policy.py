"""What the Action Service auto-decides for a Sandbox, as the app writes and shows it: the
ActionPolicyBinding it writes at launch, and the service's own answer for the Sandbox's UID.

The Action Service (x/agentplane/action_service) enforces these resources; the app writes one
binding per Sandbox it creates, from the preset's set list, and asks the service what it would
resolve at admission -- the same resolution a Decision uses, read through the operator surface.
The one thing the app adds is who wrote each binding, decided from labels the service reports and
does not interpret. Nothing here is in the decision path, and nothing edits a binding at runtime.

The kinds themselves are `x.agentplane.action_service.policy_resources`, shared with the service
that enforces them, so a set the app refuses to bind is one the service would not find either.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient
from x.agentplane.action_service.client import OperatorActionServiceClient
from x.agentplane.action_service.policy_resources import (
    BINDINGS_PLURAL,
    GROUP,
    POLICY_SETS_PLURAL,
    VERSION,
    ActionPolicySet,
    InvalidResource,
    parse_policy_set,
)
from x.agentplane.action_service.policy_view import (
    ActionPolicySetView,
    EffectivePolicyView,
    ReadyConditionView,
    SubjectActionPolicyView,
    SubjectBindingView,
)
from x.agentplane.app.action_federation import UpstreamFailure
from x.agentplane.app.egress import FLUX_KUSTOMIZATION_LABEL
from x.agentplane.app.inventory import SANDBOX_API, InventoryError

ACTION_POLICY_API = (GROUP, VERSION)
# Stamped on every binding the app writes, so a reader can tell it from one the operator wrote with
# kubectl. Which preset selected the sets is on the Sandbox's own annotation, not repeated here.
MANAGED_BY_LABEL = "app.agentplane.allegedly.works/managed-by"
MANAGED_BY_APP = "integration-app"


class UnknownPolicySetError(InventoryError):
    """A binding naming a set the namespace does not hold, which would grant nothing.

    The CRD admits any string in `spec.policySets` and the Action Service treats a name that
    resolves to nothing as contributing nothing, so a dangling name is a state the system already
    handles. This refuses one at the moment it would be written; one the operator deletes
    afterwards still lands there without granting anything from the missing set.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"no ActionPolicySet in the namespace is named {', '.join(names)}")
        self.names = names


class _ResourceList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[dict[str, Any]]


class BindingProvenance(StrEnum):
    GIT = "git"
    APP = "app"
    OPERATOR = "operator"


class ActionPolicyBindingView(BaseModel):
    """The service's binding view with its labels read into who wrote the binding."""

    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: BindingProvenance = Field(
        description="git: Flux applied it; app: this app wrote it at Sandbox creation; operator: anything else."
    )
    expires_at: datetime | None = None
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the Action Service has judged the binding at all."
    )
    policy_sets: list[ActionPolicySetView] = Field(description="The named sets that exist, in the binding's order.")
    missing_policy_sets: list[str] = Field(description="Names in the binding that no ActionPolicySet answers to.")


class ActionPolicyView(BaseModel):
    """The Action Service's answer for the Sandbox's UID: what it would evaluate at admission, from
    the objects as its informer holds them. Deny wins over approve; a request matching nothing takes
    the human path."""

    model_config = ConfigDict(extra="forbid")

    synced: bool = Field(
        description="False until the service's watch has synced: nothing auto-decides then, whatever the objects say."
    )
    bindings: list[ActionPolicyBindingView] = Field(
        description="The unexpired, valid bindings whose subject is the live Sandbox, in name order."
    )
    auto_approve_if: list[EffectivePolicyView] = Field(description="In evaluation order; the first match approves.")
    auto_deny_if: list[EffectivePolicyView]
    auto_deny_unless: list[EffectivePolicyView]


class ActionPolicyUnavailable(BaseModel):
    """The Action Service could not be asked, and the page says so rather than showing an empty
    policy as the answer: the failure an operator route would answer with, in the frame instead."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["unavailable"] = "unavailable"
    code: str = Field(
        description="A federation failure code (`operator_session_required`, `operator_federation_not_configured`, "
        "`operator_reauthentication_required`, ...), or `upstream_request_failed` when a request itself failed."
    )
    upstream: UpstreamFailure | None = Field(
        default=None, description="For `upstream_request_failed`: the request that failed, without secrets."
    )


class ActionPolicyInventory:
    """The namespace's sets and bindings: written through the API server, read back from the
    Action Service that evaluates them."""

    def __init__(self, *, namespace: str, custom_objects: CustomObjectsClient) -> None:
        self._namespace = namespace
        self._custom_objects = custom_objects

    async def require_policy_sets(self, names: Sequence[str]) -> None:
        """Every name must resolve to a set the namespace holds, or nothing is written."""
        _require_known(names, await self._policy_sets_by_name())

    async def for_sandbox(self, client: OperatorActionServiceClient, sandbox_uid: UUID) -> ActionPolicyView:
        """The Sandbox's policy as the Action Service resolves it now, for the UID a subject pins."""
        return action_policy_view(
            await client.sandbox_action_policy(namespace=self._namespace, sandbox_uid=str(sandbox_uid))
        )

    async def bind(self, *, sandbox: str, sandbox_uid: UUID, policy_sets: Sequence[str]) -> None:
        """One binding of the Sandbox to the sets, owned by the Sandbox so its deletion
        garbage-collects it. Creating it is the whole grant; the Action Service reads `spec` and
        learns nothing of which preset chose the sets.
        """
        _require_known(policy_sets, await self._policy_sets_by_name())
        await self._custom_objects.create_namespaced_custom_object(
            *ACTION_POLICY_API,
            self._namespace,
            BINDINGS_PLURAL,
            {
                "apiVersion": "/".join(ACTION_POLICY_API),
                "kind": "ActionPolicyBinding",
                "metadata": {
                    # The API server names it, as it does the egress binding: a Sandbox may be
                    # bound again later, and a name derived from the Sandbox alone would 409.
                    "generateName": f"{sandbox}-",
                    "labels": {MANAGED_BY_LABEL: MANAGED_BY_APP},
                    # Not the controller: the Sandbox controller owns the Pod and PVC, and this
                    # reference is for cascading deletion only. The binding lives in the Sandbox's
                    # namespace, which is also where the Action Service matches it to the caller,
                    # so the cascade holds.
                    "ownerReferences": [
                        {
                            "apiVersion": "/".join(SANDBOX_API),
                            "kind": "Sandbox",
                            "name": sandbox,
                            "uid": str(sandbox_uid),
                            "controller": False,
                            "blockOwnerDeletion": False,
                        }
                    ],
                },
                "spec": {
                    "subject": {"sandbox": {"name": sandbox, "uid": str(sandbox_uid)}},
                    "policySets": list(policy_sets),
                },
            },
        )

    async def _policy_sets_by_name(self) -> dict[str, ActionPolicySet | InvalidResource]:
        listed = await self._custom_objects.list_namespaced_custom_object(
            *ACTION_POLICY_API, self._namespace, POLICY_SETS_PLURAL
        )
        return {
            parsed.metadata.name: parsed for parsed in map(parse_policy_set, _ResourceList.model_validate(listed).items)
        }


def _require_known(names: Sequence[str], policy_sets: dict[str, ActionPolicySet | InvalidResource]) -> None:
    if unknown := [name for name in names if name not in policy_sets]:
        raise UnknownPolicySetError(unknown)


def action_policy_view(view: SubjectActionPolicyView) -> ActionPolicyView:
    """The service's answer with each binding's labels read into its provenance; everything else
    passes through as the service resolved it."""
    return ActionPolicyView(
        synced=view.synced,
        bindings=[_binding_view(binding) for binding in view.bindings],
        auto_approve_if=view.auto_approve_if,
        auto_deny_if=view.auto_deny_if,
        auto_deny_unless=view.auto_deny_unless,
    )


def _binding_view(binding: SubjectBindingView) -> ActionPolicyBindingView:
    return ActionPolicyBindingView(
        name=binding.name,
        provenance=_provenance(binding.labels),
        expires_at=binding.expires_at,
        ready=binding.ready,
        policy_sets=binding.policy_sets,
        missing_policy_sets=binding.missing_policy_sets,
    )


def _provenance(labels: dict[str, str]) -> BindingProvenance:
    if FLUX_KUSTOMIZATION_LABEL in labels:
        return BindingProvenance.GIT
    if labels.get(MANAGED_BY_LABEL) == MANAGED_BY_APP:
        return BindingProvenance.APP
    return BindingProvenance.OPERATOR
