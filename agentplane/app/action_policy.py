"""Read-only UI composition of Action Service policy views.

The Action Service's answer for a ServiceAccount is the auto-approval list it evaluates at admission;
the bindings that feed it are the app's own PolicyBinding view, which every reader's verdict is on.
"""

from __future__ import annotations

# Generated service annotations need the grpcio typing dependency.
# gazelle:include_dep @pypi//grpcio
from collections.abc import Mapping
from typing import Literal

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service import action_service_pb2 as action_pb, action_service_pb2_grpc
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policy_view import (
    ArgumentSchemaView,
    EffectivePolicyView,
    ExactActionsView,
    GitHubPublicRepositoryView,
    GitHubRepositoryView,
    HomeAssistantEntityControlView,
    PolicyView,
)
from agentplane.app.action_federation import UpstreamFailure
from agentplane.app.grpc_stream import OperatorGrpcClient
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
        "`operator_reauthentication_required`, ...), or `upstream_request_failed` when an Action Service RPC failed."
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

    async def for_subject(
        self,
        client: OperatorGrpcClient,
        stub: action_service_pb2_grpc.ActionServiceStub,
        bearer: str,
        subject: ServiceAccountRef,
    ) -> ActionPolicyView:
        """The subject's policy from the Action Service's generated gRPC API."""
        request = action_pb.GetActionPolicyRequest()
        request.subject.namespace = subject.namespace
        request.subject.name = subject.name
        response = await client.unary(stub.GetActionPolicy, request, bearer_token=bearer)
        if not response.HasField("action_policy"):
            raise ValueError("Action Service returned no action policy")
        policy = response.action_policy
        return ActionPolicyView(
            synced=policy.synced, auto_approve_if=[_effective_policy(item) for item in policy.auto_approve_if]
        )


def _effective_policy(effective: action_pb.EffectivePolicy) -> EffectivePolicyView:
    source = action_pb.PolicySource.Name(effective.source).removeprefix("POLICY_SOURCE_")
    return EffectivePolicyView(
        binding=effective.binding,
        policy_set=effective.policy_set,
        source=PolicySource[source],
        index=effective.index,
        policy=_policy_view(effective.policy),
    )


def _policy_view(message: action_pb.Policy) -> PolicyView:
    def actions(named: Mapping[str, action_pb.StringList]) -> dict[str, list[str]]:
        return {group: list(names.values) for group, names in named.items()}

    match message.WhichOneof("kind"):
        case "exact_actions":
            exact_policy = message.exact_actions
            return ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions=actions(exact_policy.actions))
        case "argument_schema":
            argument_policy = message.argument_schema
            return ArgumentSchemaView(
                type=PolicyKind.ARGUMENT_SCHEMA,
                actions=actions(argument_policy.actions),
                argument_schema=MessageToDict(argument_policy.argument_schema, preserving_proto_field_name=True),
            )
        case "github_repository":
            repository_policy = message.github_repository
            return GitHubRepositoryView(
                type=PolicyKind.GITHUB_REPOSITORY,
                actions=actions(repository_policy.actions),
                owner=repository_policy.owner,
                repository=repository_policy.repository,
            )
        case "github_public_repository":
            public_repository_policy = message.github_public_repository
            return GitHubPublicRepositoryView(
                type=PolicyKind.GITHUB_PUBLIC_REPOSITORY, actions=actions(public_repository_policy.actions)
            )
        case "home_assistant_entity_control":
            entity_policy = message.home_assistant_entity_control
            return HomeAssistantEntityControlView(
                type=PolicyKind.HOME_ASSISTANT_ENTITY_CONTROL,
                actions=actions(entity_policy.actions),
                entities={entity: list(names.values) for entity, names in entity_policy.entities.items()},
            )
        case _:
            raise ValueError("Action Service returned a policy without a recognized kind")
