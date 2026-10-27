"""How the app reads the Action Service's answer for a ServiceAccount."""

from __future__ import annotations

# Generated service annotations need the grpcio typing dependency.
# gazelle:include_dep @pypi//grpcio
# Generated protobuf annotations need the protobuf typing dependency.
# gazelle:include_dep @pypi//protobuf
import pytest_bazel

from agentplane.action_service import action_service_pb2, action_service_pb2_grpc
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policy_view import EffectivePolicyView, ExactActionsView
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.testing.fake_action_service import FakeActionService
from agentplane.subjects import ServiceAccountRef

LIVE = ServiceAccountRef(namespace="test-sandboxes", name="live")


async def test_for_subject_asks_the_service_about_the_account_and_passes_on_its_list(
    action_policy: ActionPolicyInventory,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    fake, stub, client = action_service
    policy = action_service_pb2.CallerActionPolicy(synced=True)
    effective = policy.auto_approve_if.add(
        binding="live-k2m9x", policy_set="reads", source=action_service_pb2.POLICY_SOURCE_RULES
    )
    effective.policy.exact_actions.actions["github"].values.append("search_code")
    fake.policy = action_service_pb2.GetActionPolicyResponse(action_policy=policy)
    view = await action_policy.for_subject(client, stub, "test-federated-token", LIVE)

    assert len(fake.calls) == 1
    assert fake.calls[0].method == "GetActionPolicy"
    assert isinstance(fake.calls[0].request, action_service_pb2.GetActionPolicyRequest)
    assert fake.calls[0].request.subject.namespace == "test-sandboxes"
    assert fake.calls[0].request.subject.name == "live"
    assert fake.calls[0].bearer_token == "Bearer test-federated-token"
    assert view.synced is True
    assert view.auto_approve_if == [
        EffectivePolicyView(
            binding="live-k2m9x",
            policy_set="reads",
            source=PolicySource.RULES,
            index=0,
            policy=ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions={"github": ["search_code"]}),
        )
    ]


if __name__ == "__main__":
    pytest_bazel.main()
