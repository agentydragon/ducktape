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
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import (
    ConfiguredOperatorBearerAuthenticator,
    DisabledOperatorAuthenticator,
    OperatorAuthenticator,
    workload_principal,
)
from x.agentplane.action_service.client import (
    WORKLOAD_CREDENTIAL_PLACEHOLDER,
    ActionServiceClient,
    CredentialPlaceholder,
)
from x.agentplane.action_service.models import ActionRequestInput, ActionRequestView, ActionState, Principal
from x.agentplane.action_service.service import ActionService
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    SandboxPrincipal,
    SandboxPrincipalResolver,
)

AUDIENCE = "agentplane-egress"
NAMESPACE = "agentplane-staging"
SUBJECT = f"system:serviceaccount:{NAMESPACE}:agentplane-runner"
TOKEN_A = "opaque-bound-workload-a"
TOKEN_B = "opaque-bound-workload-b"


def principal(label: str) -> SandboxPrincipal:
    return SandboxPrincipal(
        namespace=NAMESPACE,
        service_account_name="agentplane-runner",
        service_account_subject=SUBJECT,
        pod_name=f"sandbox-{label}-pod",
        pod_uid=f"pod-{label}-uid",
        sandbox_name=f"sandbox-{label}",
        sandbox_uid=f"sandbox-{label}-uid",
    )


PRINCIPAL_A = principal("a")
PRINCIPAL_B = principal("b")


def review(token: str, resolved: SandboxPrincipal, *, audience: str = AUDIENCE) -> k8s_client.V1TokenReview:
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


def pod(resolved: SandboxPrincipal) -> k8s_client.V1Pod:
    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            namespace=resolved.namespace,
            name=resolved.pod_name,
            uid=resolved.pod_uid,
            owner_references=[
                k8s_client.V1OwnerReference(
                    api_version="agents.x-k8s.io/v1beta1",
                    kind="Sandbox",
                    name=resolved.sandbox_name,
                    uid=resolved.sandbox_uid,
                    controller=True,
                )
            ],
        )
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


class FakeCoreApi:
    def __init__(self) -> None:
        self.pods = {(p.namespace, p.pod_name): pod(p) for p in (PRINCIPAL_A, PRINCIPAL_B)}

    async def read_namespaced_pod(self, name: str, namespace: str) -> k8s_client.V1Pod:
        return self.pods[(namespace, name)]


def workload_authenticator() -> tuple[SandboxPrincipalAuthenticator, FakeAuthenticationApi]:
    authentication = FakeAuthenticationApi()
    resolver = SandboxPrincipalResolver(
        authentication=cast(AuthenticationV1Api, authentication),
        core_v1=cast(CoreV1Api, FakeCoreApi()),
        audience=AUDIENCE,
        namespaces=frozenset({NAMESPACE}),
    )
    return SandboxPrincipalAuthenticator(resolver), authentication


class RecordingActionService:
    def __init__(self) -> None:
        self.principals: list[Principal] = []
        self.bodies: list[ActionRequestInput] = []

    async def submit(self, body: ActionRequestInput, principal_value: Principal) -> ActionRequestView:
        self.principals.append(principal_value)
        self.bodies.append(body)
        now = datetime.now(UTC)
        return ActionRequestView(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            idempotency_key=body.idempotency_key,
            capability=body.capability,
            arguments=body.arguments,
            origin=body.origin,
            correlation=body.correlation,
            caller_principal=None,
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


async def test_same_service_account_pods_resolve_two_sandbox_principals() -> None:
    authenticator, authentication = workload_authenticator()

    first, second = await authenticator.resolver.resolve(TOKEN_A), await authenticator.resolver.resolve(TOKEN_B)

    assert first.service_account_subject == second.service_account_subject == SUBJECT
    assert (first.pod_uid, first.sandbox_uid) != (second.pod_uid, second.sandbox_uid)
    assert authentication.seen_tokens == [TOKEN_A, TOKEN_B]


async def test_central_placeholder_replay_is_required_before_action_service_auth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    authenticator, authentication = workload_authenticator()
    service = RecordingActionService()
    app = create_app(
        cast(ActionService, service), authenticator, cast(OperatorAuthenticator, DisabledOperatorAuthenticator())
    )
    body = ActionRequestInput(
        idempotency_key="central-replay",
        capability="agentplane:v0.echo",
        arguments={"text": "hello"},
        origin={"sandbox_id": PRINCIPAL_B.sandbox_uid, "thread_id": "untrusted"},
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


async def test_operator_adapter_is_distinct_digest_only_and_file_configured(tmp_path: Path) -> None:
    path = tmp_path / "operator-bearer"
    path.write_text("opaque-bff-bearer\n")
    authenticator = ConfiguredOperatorBearerAuthenticator.from_file(path, subject="haku-bff")

    accepted = await authenticator.authenticate("opaque-bff-bearer")

    assert accepted is not None
    assert accepted.key == "configured-operator:haku-bff"
    assert await authenticator.authenticate("wrong") is None
    assert "opaque-bff-bearer" not in repr(authenticator.__dict__)
    assert await DisabledOperatorAuthenticator().authenticate("anything") is None


if __name__ == "__main__":
    pytest_bazel.main()
