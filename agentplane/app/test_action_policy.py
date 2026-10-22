"""How the app reads the Action Service's answer for a ServiceAccount."""

from __future__ import annotations

import httpx
import pytest_bazel
from google.protobuf.json_format import MessageToJson

from agentplane.action_service import action_service_pb2 as action_pb
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policy_view import EffectivePolicyView, ExactActionsView
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.upstream_proxy import UpstreamProxy
from agentplane.subjects import ServiceAccountRef

LIVE = ServiceAccountRef(namespace="test-sandboxes", name="live")


async def test_for_subject_asks_the_service_about_the_account_and_passes_on_its_list(
    action_policy: ActionPolicyInventory,
) -> None:
    asked: list[httpx.Request] = []

    policy = action_pb.CallerActionPolicy(synced=True)
    effective = policy.auto_approve_if.add(
        binding="live-k2m9x", policy_set="reads", source=action_pb.POLICY_SOURCE_RULES
    )
    effective.policy.exact_actions.actions["github"].values.append("search_code")
    reply = action_pb.GetActionPolicyResponse(action_policy=policy)

    def service(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        assert request.headers["Authorization"] == "Bearer test-federated-token"
        return httpx.Response(200, text=MessageToJson(reply))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(service), base_url="http://test-actions.invalid"
    ) as client:
        view = await action_policy.for_subject(UpstreamProxy(client, token_file=None), "test-federated-token", LIVE)

    assert len(asked) == 1
    assert asked[0].url.path == "/v1/action-policy"
    assert asked[0].url.query == b"subject.namespace=test-sandboxes&subject.name=live"
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
