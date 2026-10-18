"""How the app reads the Action Service's answer for a ServiceAccount."""

from __future__ import annotations

import httpx
import pytest_bazel

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policy_view import EffectivePolicyView, ExactActionsView, SubjectActionPolicyView
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.conftest import operator_token
from agentplane.sandboxes.testing.fake_inventory import NAMESPACE
from agentplane.subjects import ServiceAccountRef

LIVE = ServiceAccountRef(namespace=NAMESPACE, name="live")

ANSWER = SubjectActionPolicyView(
    synced=True,
    bindings=[],
    auto_approve_if=[
        EffectivePolicyView(
            binding="live-k2m9x",
            policy_set="reads",
            source=PolicySource.RULES,
            index=0,
            policy=ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions={"github": ["search_code"]}),
        )
    ],
)


async def test_for_subject_asks_the_service_about_the_account_and_passes_on_its_list(
    action_policy: ActionPolicyInventory,
) -> None:
    asked: list[str] = []

    def service(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json=ANSWER.model_dump(mode="json"))

    client = OperatorActionServiceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(service), base_url="http://test-actions.invalid"),
        operator_token,
    )

    view = await action_policy.for_subject(client, LIVE)

    assert asked == [f"/v1/operator/action-policy/service-accounts/{NAMESPACE}/live"]
    assert (view.synced, view.auto_approve_if) == (True, ANSWER.auto_approve_if)


if __name__ == "__main__":
    pytest_bazel.main()
