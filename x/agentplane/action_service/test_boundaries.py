"""Shared Sandbox auth, central placeholder replay, and distinct operator/BFF boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import (
    ConfiguredOperatorBearerAuthenticator,
    DisabledOperatorAuthenticator,
    OperatorAuthenticator,
    workload_principal,
)
from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.client import (
    WORKLOAD_CREDENTIAL_PLACEHOLDER,
    ActionServiceClient,
    CredentialPlaceholder,
)
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    OperatorPrincipal,
)
from x.agentplane.action_service.policies.resources import CALLER_LABEL
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.test_fixtures.callers import admitted_callers
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.subjects import ServiceAccountRef
from x.agentplane.workload_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    WorkloadPrincipal,
    WorkloadPrincipalResolver,
)

AUDIENCE = "agentplane-egress"
NAMESPACE = "agentplane-staging"
SUBJECT = f"system:serviceaccount:{NAMESPACE}:agentplane-runner"
TOKEN_A = "opaque-bound-workload-a"
TOKEN_B = "opaque-bound-workload-b"


def principal(label: str) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        namespace=NAMESPACE,
        service_account_name="agentplane-runner",
        service_account_subject=SUBJECT,
        pod_name=f"sandbox-{label}-pod",
        pod_uid=f"pod-{label}-uid",
    )


PRINCIPAL_A = principal("a")
PRINCIPAL_B = principal("b")


def review(token: str, resolved: WorkloadPrincipal, *, audience: str = AUDIENCE) -> k8s_client.V1TokenReview:
    return k8s_client.V1TokenReview(
        spec=k8s_client.V1TokenReviewSpec(token=token, audiences=[AUDIENCE]),
        status=k8s_client.V1TokenReviewStatus(
            authenticated=True,
            audiences=[audience],
            user=k8s_client.V1UserInfo(
                username=resolved.service_account_subject,
                extra={POD_NAME_CLAIM: [resolved.pod_name], POD_UID_CLAIM: [resolved.pod_uid]},
            ),
        ),
    )


class FakeAuthenticationApi:
    def __init__(self) -> None:
        self.reviews = {TOKEN_A: review(TOKEN_A, PRINCIPAL_A), TOKEN_B: review(TOKEN_B, PRINCIPAL_B)}
        self.seen_tokens: list[str] = []

    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        self.seen_tokens.append(body.spec.token)
        return self.reviews.get(
            body.spec.token,
            k8s_client.V1TokenReview(
                spec=body.spec, status=k8s_client.V1TokenReviewStatus(authenticated=False, audiences=[])
            ),
        )


def workload_resolver() -> tuple[WorkloadPrincipalResolver, FakeAuthenticationApi]:
    authentication = FakeAuthenticationApi()
    resolver = WorkloadPrincipalResolver(
        authentication=cast(AuthenticationV1Api, authentication),
        audience=AUDIENCE,
        allowed_service_account_namespaces=frozenset({NAMESPACE}),
    )
    return resolver, authentication


class RecordingActionService:
    draining = False

    def __init__(self) -> None:
        self.principals: list[CallerPrincipal] = []
        self.bodies: list[ActionRequestInput] = []

    async def submit(self, body: ActionRequestInput, principal_value: CallerPrincipal) -> ActionRequestView:
        self.principals.append(principal_value)
        self.bodies.append(body)
        now = datetime.now(UTC)
        return ActionRequestView(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            idempotency_key=body.idempotency_key,
            action=body.action,
            arguments=body.arguments,
            title=body.title,
            description=body.description,
            origin=body.origin,
            correlation=body.correlation,
            caller=None,
            state=ActionState.DECISION_PENDING,
            version=1,
            created_at=now,
            updated_at=now,
            decision=None,
            execution=None,
        )


class FakeCentralProxy(httpx.AsyncBaseTransport):
    """Replay the merged generic egress contract without retaining or returning the bearer."""

    def __init__(self, app: Any, workload_token: str) -> None:
        self._upstream = httpx.ASGITransport(app=app)
        self._workload_token = workload_token
        self.placeholders_seen = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER}"
        self.placeholders_seen += 1
        headers = request.headers.copy()
        headers["authorization"] = f"Bearer {self._workload_token}"
        forwarded = httpx.Request(request.method, request.url, headers=headers, content=request.content)
        return await self._upstream.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self._upstream.aclose()


async def test_same_service_account_pods_are_one_caller_from_two_bearers() -> None:
    resolver, authentication = workload_resolver()

    first, second = await resolver.resolve_workload(TOKEN_A), await resolver.resolve_workload(TOKEN_B)

    assert first.service_account_subject == second.service_account_subject == SUBJECT
    assert first.pod_uid != second.pod_uid
    assert authentication.seen_tokens == [TOKEN_A, TOKEN_B]


async def test_central_placeholder_replay_is_required_before_action_service_auth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver, authentication = workload_resolver()
    service = RecordingActionService()
    app = create_app(
        cast(ActionService, service),
        resolver,
        cast(OperatorAuthenticator, DisabledOperatorAuthenticator()),
        ActionCatalog(),
        callers=admitted_callers(ServiceAccountRef(namespace=NAMESPACE, name=PRINCIPAL_A.service_account_name)),
        updates=ActionUpdates("postgresql://unused-test-listener"),
    )
    body = ActionRequestInput(
        idempotency_key="central-replay",
        title="test title for central-replay",
        action=ActionIdentity(group="agentplane", name="echo"),
        arguments={"text": "hello"},
        origin={"sandbox_id": "forged-sandbox-uid", "thread_id": "untrusted"},
    )

    proxy = FakeCentralProxy(app, TOKEN_A)
    async with httpx.AsyncClient(transport=proxy, base_url="http://agentplane-actions") as proxied_http:
        response = await ActionServiceClient(proxied_http, CredentialPlaceholder()).submit(body)
    assert response.state is ActionState.DECISION_PENDING
    assert proxy.placeholders_seen == 1
    assert service.principals == [workload_principal(PRINCIPAL_A)]
    assert service.bodies == [body]

    # Bypassing central leaves only a public placeholder, while missing/wrong identities also fail.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agentplane-actions"
    ) as direct:
        assert (
            await direct.post(
                "/v1/action-requests",
                headers={"Authorization": f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER}"},
                json=body.model_dump(mode="json"),
            )
        ).status_code == 401
        assert (await direct.post("/v1/action-requests", json=body.model_dump(mode="json"))).status_code == 401
        assert (
            await direct.post(
                "/v1/action-requests",
                headers={"Authorization": "Bearer wrong-workload"},
                json=body.model_dump(mode="json"),
            )
        ).status_code == 401

    # TokenReview sees the post-substitution bearer, but it is absent from bodies, principals,
    # responses, and logs. The public placeholder may safely appear at the runner boundary.
    assert authentication.seen_tokens == [TOKEN_A, WORKLOAD_CREDENTIAL_PLACEHOLDER, "wrong-workload"]
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN_A not in rendered
    assert TOKEN_A not in response.model_dump_json()
    assert TOKEN_A not in str(service.bodies)
    assert TOKEN_A not in str(service.principals)


async def test_an_unlabelled_account_authenticates_and_reaches_no_route(caplog: pytest.LogCaptureFixture) -> None:
    """The label is admission, separate from proving who you are: a bearer TokenReview accepts still
    reaches nothing while the index does not list its account, so a workload the operator has not
    named cannot even queue an Action for them to decide."""
    caplog.set_level("WARNING", logger="x.agentplane.action_service.api")
    resolver, authentication = workload_resolver()
    service = RecordingActionService()
    app = create_app(
        cast(ActionService, service),
        resolver,
        cast(OperatorAuthenticator, DisabledOperatorAuthenticator()),
        ActionCatalog(),
        callers=admitted_callers(),
        updates=ActionUpdates("postgresql://unused-test-listener"),
    )
    body = ActionRequestInput(
        idempotency_key="unlabelled",
        title="test title for unlabelled",
        action=ActionIdentity(group="agentplane", name="echo"),
        arguments={"text": "hello"},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agentplane-actions") as http:
        refused = await http.post(
            "/v1/action-requests", headers={"Authorization": f"Bearer {TOKEN_A}"}, json=body.model_dump(mode="json")
        )

    assert refused.status_code == 401
    assert authentication.seen_tokens == [TOKEN_A], "the bearer was proven; what failed is admission"
    assert service.principals == []
    assert service.bodies == []
    # Opaque to the caller, explicit in the log: the operator needs to know which account to label.
    assert "invalid workload bearer" in refused.text
    assert CALLER_LABEL not in refused.text
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert PRINCIPAL_A.service_account_name in rendered
    assert CALLER_LABEL in rendered


async def test_operator_adapter_is_distinct_digest_only_and_file_configured(tmp_path: Path) -> None:
    path = tmp_path / "operator-bearer"
    path.write_text("opaque-bff-bearer\n")
    authenticator = ConfiguredOperatorBearerAuthenticator.from_file(path, subject="haku-bff")

    accepted = await authenticator.authenticate("opaque-bff-bearer")

    assert accepted is not None
    assert accepted == OperatorPrincipal(issuer="configured-operator", subject="haku-bff")
    assert await authenticator.authenticate("wrong") is None
    assert "opaque-bff-bearer" not in repr(authenticator.__dict__)
    await DisabledOperatorAuthenticator().authenticate("anything")


if __name__ == "__main__":
    pytest_bazel.main()
