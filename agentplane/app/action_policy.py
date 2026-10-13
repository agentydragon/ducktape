"""Read-only UI composition of Action Service policy views.

Sandbox Service owns launch-time policy binding mutations. The app adds provenance to the
Action Service's answer; the historical integration-app ownership label is retained in staging.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.policy_view import (
    ActionPolicySetView,
    EffectivePolicyView,
    ReadyConditionView,
    SubjectActionPolicyView,
    SubjectBindingView,
)
from agentplane.action_service.providers import BindingKind
from agentplane.app.action_federation import UpstreamFailure
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.sandbox_service.action_policy_views import MANAGED_BY_APP, MANAGED_BY_LABEL, ActionPolicyReader
from agentplane.sandbox_service.egress_views import FLUX_KUSTOMIZATION_LABEL
from agentplane.subjects import ServiceAccountRef


class BindingProvenance(StrEnum):
    GIT = "git"
    APP = "app"
    OPERATOR = "operator"


class ActionPolicyBindingView(BaseModel):
    """The service's binding view with its labels read into who wrote the binding."""

    model_config = ConfigDict(extra="forbid")

    kind: BindingKind
    name: str
    provenance: BindingProvenance = Field(
        description="git: Flux applied it; app: this app wrote it at Sandbox creation; operator: anything else."
    )
    generation: int
    expires_at: datetime | None = None
    ready: ReadyConditionView | None = Field(
        default=None, description="Absent until the Action Service has judged the binding at all."
    )
    policy_sets: list[ActionPolicySetView] = Field(description="The named sets that exist, in the binding's order.")
    missing_policy_sets: list[str] = Field(description="Names in the binding that no PolicySet answers to.")


class ActionPolicyView(BaseModel):
    """The Action Service's answer for a ServiceAccount: what it would evaluate at admission, from
    the objects as its informer holds them. Deny wins over approve; a request matching nothing takes
    the human path."""

    model_config = ConfigDict(extra="forbid")

    synced: bool = Field(
        description="False until the service's copy is complete, and again if its watch stops refreshing it: "
        "nothing auto-decides then, whatever the objects say."
    )
    bindings: list[ActionPolicyBindingView] = Field(
        description="The unexpired, valid bindings whose subject is the requested ServiceAccount, in name order."
    )
    auto_approve_if: list[EffectivePolicyView] = Field(description="In evaluation order; the first match approves.")


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


class ActionPolicyInventory(ActionPolicyReader):
    """Read-only policy inventory and operator-federated policy views."""

    async def policy_set_names(self) -> list[str]:
        """The namespace's PolicySets, of any rule type, in name order."""
        listed = await self._custom_objects.list_namespaced_custom_object(
            GROUP, VERSION, self._namespace, POLICY_SETS_PLURAL
        )
        return sorted(item["metadata"]["name"] for item in listed["items"])

    async def for_subject(self, client: OperatorActionServiceClient, subject: ServiceAccountRef) -> ActionPolicyView:
        """The subject's policy as the Action Service resolves it now."""
        return action_policy_view(await client.service_account_action_policy(subject))


def action_policy_view(view: SubjectActionPolicyView) -> ActionPolicyView:
    """The service's answer with each binding's labels read into its provenance; everything else
    passes through as the service resolved it."""
    return ActionPolicyView(
        synced=view.synced,
        bindings=[_binding_view(binding) for binding in view.bindings],
        auto_approve_if=view.auto_approve_if,
    )


def _binding_view(binding: SubjectBindingView) -> ActionPolicyBindingView:
    return ActionPolicyBindingView(
        kind=binding.kind,
        name=binding.name,
        provenance=_provenance(binding.labels),
        generation=binding.generation,
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
