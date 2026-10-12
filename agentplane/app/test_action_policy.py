"""What the app writes for a new Sandbox, and how it reads the Action Service's answer back."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
import pytest_bazel

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.action_service.models import PolicyKind, PolicySource
from agentplane.action_service.policy_view import (
    ActionPolicySetView,
    EffectivePolicyView,
    ExactActionsView,
    ReadyConditionView,
    SubjectActionPolicyView,
    SubjectBindingView,
)
from agentplane.action_service.providers import BindingKind
from agentplane.app.action_policy import ActionPolicyInventory, BindingProvenance
from agentplane.policy_sets import ConditionStatus
from agentplane.sandbox_service.action_policy_views import MANAGED_BY_APP, MANAGED_BY_LABEL
from agentplane.sandbox_service.egress_views import FLUX_KUSTOMIZATION_LABEL
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCustomObjectsApi
from agentplane.subjects import ServiceAccountRef

LIVE = ServiceAccountRef(namespace=NAMESPACE, name="live")


@pytest.fixture
def inventory(custom_objects: FakeCustomObjectsApi) -> ActionPolicyInventory:
    return ActionPolicyInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects))


READY = ReadyConditionView(status=ConditionStatus.TRUE, reason="Valid", message="spec accepted", observed_generation=1)
ANSWER = SubjectActionPolicyView(
    synced=True,
    bindings=[
        SubjectBindingView(
            kind=BindingKind.POLICY_BINDING,
            name="live-afternoon",
            labels={},
            expires_at=datetime(2026, 9, 12, 20, tzinfo=UTC),
            ready=None,
            policy_sets=[ActionPolicySetView(name="issues", generation=1)],
            missing_policy_sets=["vanished"],
        ),
        SubjectBindingView(
            kind=BindingKind.ACTION_POLICY_BINDING,
            name="live-k2m9x",
            labels={MANAGED_BY_LABEL: MANAGED_BY_APP},
            ready=READY,
            policy_sets=[ActionPolicySetView(name="reads", generation=3, ready=READY)],
            missing_policy_sets=[],
        ),
        SubjectBindingView(
            kind=BindingKind.ACTION_POLICY_BINDING,
            name="live-seeded",
            labels={FLUX_KUSTOMIZATION_LABEL: "test-actions", MANAGED_BY_LABEL: "someone-else"},
            ready=READY,
            policy_sets=[ActionPolicySetView(name="reads", generation=3, ready=READY)],
            missing_policy_sets=[],
        ),
    ],
    auto_approve_if=[
        EffectivePolicyView(
            binding="live-k2m9x",
            policy_set="reads",
            source=PolicySource.AUTO_APPROVE_IF,
            index=0,
            policy=ExactActionsView(type=PolicyKind.EXACT_ACTIONS, actions={"github": ["search_code"]}),
        )
    ],
)


async def token() -> str:
    return "test-operator-token"


async def test_for_subject_asks_the_service_and_adds_only_who_wrote_each_binding(
    inventory: ActionPolicyInventory,
) -> None:
    """The live frame goes through here: the service's answer for the ServiceAccount the sandbox
    runs as, with labels read into provenance and nothing else re-derived."""
    asked: list[str] = []

    def service(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json=ANSWER.model_dump(mode="json"))

    client = OperatorActionServiceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(service), base_url="http://test-actions.invalid"), token
    )

    view = await inventory.for_subject(client, LIVE)

    assert asked == [f"/v1/operator/action-policy/service-accounts/{NAMESPACE}/live"]
    assert [(b.name, b.provenance) for b in view.bindings] == [
        ("live-afternoon", BindingProvenance.OPERATOR),
        ("live-k2m9x", BindingProvenance.APP),
        ("live-seeded", BindingProvenance.GIT),
    ]
    afternoon, app_written, _ = view.bindings
    assert (afternoon.kind, afternoon.expires_at, afternoon.ready, afternoon.missing_policy_sets) == (
        BindingKind.POLICY_BINDING,
        datetime(2026, 9, 12, 20, tzinfo=UTC),
        None,
        ["vanished"],
    )
    assert app_written.policy_sets == ANSWER.bindings[1].policy_sets
    assert (view.synced, view.auto_approve_if) == (True, ANSWER.auto_approve_if)


if __name__ == "__main__":
    pytest_bazel.main()
