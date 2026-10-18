"""Read-only UI composition of Action Service policy views.

The Action Service's answer for a ServiceAccount is the auto-approval list it evaluates at admission;
the bindings that feed it are the app's own PolicyBinding view, which every reader's verdict is on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.policy_view import EffectivePolicyView
from agentplane.app.action_federation import UpstreamFailure
from agentplane.crd_group import GROUP, VERSION
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.sandboxes.action_policy_views import ActionPolicyReader
from agentplane.subjects import ServiceAccountRef


class ActionPolicyView(BaseModel):
    """The Action Service's answer for a ServiceAccount: what it would evaluate at admission, from
    the objects as its informer holds them. Deny wins over approve; a request matching nothing takes
    the human path."""

    model_config = ConfigDict(extra="forbid")

    synced: bool = Field(
        description="False until the service's copy is complete, and again if its watch stops refreshing it: "
        "nothing auto-decides then, whatever the objects say."
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
        view = await client.service_account_action_policy(subject)
        return ActionPolicyView(synced=view.synced, auto_approve_if=view.auto_approve_if)
