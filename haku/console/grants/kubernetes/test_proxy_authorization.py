import asyncio
import datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.grants.kubernetes.authorization import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    AuthorizationRequest,
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    KubernetesAuthorizationUnavailableError,
    KubernetesBearerRejectedError,
    KubernetesClients,
    KubernetesSubjectAccessReviewClient,
    RequestAttributes,
    SubjectAccessReviewResult,
)
from haku.console.grants.kubernetes.models import GrantDecision, GrantScopeKind
from haku.console.grants.kubernetes.proxy_authorization import router
from haku.console.grants.kubernetes.service import GrantService
from haku.console.grants.principal import RequestPrincipal
from haku.console.tool_call_actor import AgentActor

REQUEST = {
    "attributes": {
        "resource_request": True,
        "verb": "get",
        "api_version": "v1",
        "namespace": "demo",
        "resource": "pods",
        "subresource": "log",
        "name": "web",
        "path": "/api/v1/namespaces/demo/pods/web/log",
    },
    "required_scope": {"kind": "namespaces", "namespaces": ["demo"]},
    "required_rules": [{"api_groups": [""], "resources": ["pods/log"], "verbs": ["get"], "resource_names": ["web"]}],
}


def _client(service: KubernetesAuthorizationService | None = None) -> TestClient:
    app = FastAPI()
    if service is not None:
        app.state.kubernetes_authorization = service
    app.include_router(router)
    return TestClient(app)


def test_endpoint_requires_bearer() -> None:
    with _client() as client:
        response = client.post("/api/internal/kubernetes/authorize", json=REQUEST)
    assert response.status_code == 401


def test_endpoint_is_unavailable_when_not_wired() -> None:
    with _client() as client:
        response = client.post(
            "/api/internal/kubernetes/authorize", json=REQUEST, headers={"Authorization": "Bearer agent-token"}
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "Kubernetes authorization is not configured"


class FakeSarClient:
    def __init__(self, result: SubjectAccessReviewResult | None = None, error: Exception | None = None) -> None:
        self.result = result or SubjectAccessReviewResult(allowed=True, reason="standing policy")
        self.error = error
        self.calls: list[tuple[KubernetesAuthorizationSubject, RequestAttributes]] = []
        self.closed = False

    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult:
        self.calls.append((subject, attributes))
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed = True


class FakeAuthorizationApi:
    def __init__(self, *, evaluation_error: str | None = None) -> None:
        self.requests: list[Any] = []
        self.evaluation_error = evaluation_error

    async def create_subject_access_review(self, request):
        self.requests.append(request)
        return type(
            "Response",
            (),
            {
                "status": type(
                    "Status",
                    (),
                    {
                        "allowed": True,
                        "denied": False,
                        "reason": "standing policy",
                        "evaluation_error": self.evaluation_error,
                    },
                )()
            },
        )()


class FakeApiClient:
    async def close(self) -> None:
        pass


def _agent() -> AgentActor:
    return AgentActor(
        agent_id=UUID("00000000-0000-4000-8000-000000000001"),
        operator_id=UUID("00000000-0000-4000-8000-000000000002"),
        binding_id=UUID("00000000-0000-4000-8000-000000000003"),
        access_profile_id="public-diagnostics",
    )


class FakeAgentBearerAuthority:
    def __init__(self, actor: AgentActor | None = None) -> None:
        self.actor = _agent() if actor is None else actor

    async def authenticate(self, token: str) -> AgentActor | None:
        del token
        return self.actor


class RejectingAgentBearerAuthority:
    async def authenticate(self, token: str) -> None:
        del token


def _service(
    sar: FakeSarClient,
    bearer_authority: FakeAgentBearerAuthority | RejectingAgentBearerAuthority | None = None,
    grants: Any = None,
) -> KubernetesAuthorizationService:
    bearer_authority = bearer_authority or FakeAgentBearerAuthority()
    if grants is None:
        grants = AsyncMock()
        grants.match_request.return_value = GrantDecision(allowed=False)
    return KubernetesAuthorizationService(
        config=KubernetesAuthorizationConfig(
            subjects_by_access_profile={
                "public-diagnostics": KubernetesAuthorizationSubject(
                    username="system:serviceaccount:haku:haku-kube-proxy", groups=("haku",)
                )
            }
        ),
        agent_bearer_authority=cast(Any, bearer_authority),
        sar_client=sar,
        grants=grants,
    )


class EmptyGrantRepository:
    async def active_for_request_principal(self, *, request_principal, now):
        return ()


@pytest.mark.asyncio
async def test_service_uses_fixed_configured_subject_and_request_attributes() -> None:
    sar = FakeSarClient()
    result = await _service(sar).authorize(
        bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST)
    )
    assert result.allowed is True
    assert sar.calls == [
        (
            KubernetesAuthorizationSubject(username="system:serviceaccount:haku:haku-kube-proxy", groups=("haku",)),
            RequestAttributes.model_validate(REQUEST["attributes"]),
        )
    ]


@pytest.mark.asyncio
async def test_service_fails_closed_when_agent_profile_has_no_configured_subject() -> None:
    sar = FakeSarClient()

    unconfigured_profile = FakeAgentBearerAuthority(
        AgentActor(
            agent_id=UUID("00000000-0000-4000-8000-000000000011"),
            operator_id=UUID("00000000-0000-4000-8000-000000000012"),
            binding_id=UUID("00000000-0000-4000-8000-000000000013"),
            access_profile_id="unconfigured",
        )
    )

    with pytest.raises(KubernetesAuthorizationUnavailableError, match="Agent access profile"):
        await _service(sar, unconfigured_profile).authorize(
            bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST)
        )
    assert sar.calls == []


def test_endpoint_returns_sar_decision() -> None:
    sar = FakeSarClient(result=SubjectAccessReviewResult(allowed=False, reason="RBAC denied"))
    with _client(_service(sar)) as client:
        response = client.post(
            "/api/internal/kubernetes/authorize", json=REQUEST, headers={"Authorization": "Bearer caller-token"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is False
    assert body["reason"] == "RBAC denied"
    assert body["source"] == "sar"
    assert body["decision_id"].startswith("sar:")


@pytest.mark.asyncio
async def test_clean_sar_denial_with_real_empty_grant_service_remains_denied() -> None:
    grants = GrantService(
        cast(Any, EmptyGrantRepository()),
        max_lifetime=datetime.timedelta(hours=1),
        clock=lambda: datetime.datetime(2026, 8, 23, tzinfo=datetime.UTC),
    )
    result = await _service(
        FakeSarClient(result=SubjectAccessReviewResult(allowed=False, reason="RBAC denied")), grants=grants
    ).authorize(bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST))

    assert result.allowed is False
    assert result.reason == "RBAC denied"
    assert result.source is KubernetesAuthorizationSource.SAR


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_scope", {"kind": "namespaces", "namespaces": ["other"]}),
        ("required_rules", [{"api_groups": [""], "resources": ["secrets"], "verbs": ["get"]}]),
    ],
)
def test_endpoint_rejects_noncanonical_scope_or_rules(field: str, value: object) -> None:
    request = {**REQUEST, field: value}
    with _client(_service(FakeSarClient())) as client:
        response = client.post(
            "/api/internal/kubernetes/authorize", json=request, headers={"Authorization": "Bearer caller-token"}
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_service_rejects_unknown_bearer_before_sar() -> None:
    sar = FakeSarClient()

    with pytest.raises(KubernetesBearerRejectedError):
        await _service(sar, RejectingAgentBearerAuthority()).authorize(
            bearer="Bearer unknown", request=AuthorizationRequest.model_validate(REQUEST)
        )
    assert sar.calls == []


@pytest.mark.asyncio
async def test_service_surfaces_sar_failure_as_unavailable() -> None:
    sar = FakeSarClient(error=KubernetesAuthorizationUnavailableError("SAR failed"))
    with pytest.raises(KubernetesAuthorizationUnavailableError, match="SAR failed"):
        await _service(sar).authorize(
            bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST)
        )


@pytest.mark.asyncio
async def test_subject_access_review_client_builds_resource_request_for_fixed_subject() -> None:
    authorization = FakeAuthorizationApi()
    client = KubernetesSubjectAccessReviewClient(
        clients=KubernetesClients(api=FakeApiClient(), authorization=authorization)
    )
    result = await client.review(
        subject=KubernetesAuthorizationSubject(username="proxy", groups=("haku",)),
        attributes=RequestAttributes.model_validate(REQUEST["attributes"]),
    )
    assert result == SubjectAccessReviewResult(allowed=True, reason="standing policy")
    request = authorization.requests[0]
    assert request.spec.user == "proxy"
    assert request.spec.groups == ["haku"]
    assert request.spec.resource_attributes.resource == "pods"
    assert request.spec.resource_attributes.subresource == "log"
    assert request.spec.non_resource_attributes is None


@pytest.mark.asyncio
async def test_subject_access_review_client_fails_closed_on_evaluation_error() -> None:
    client = KubernetesSubjectAccessReviewClient(
        clients=KubernetesClients(
            api=FakeApiClient(), authorization=FakeAuthorizationApi(evaluation_error="authorizer unavailable")
        )
    )
    with pytest.raises(KubernetesAuthorizationUnavailableError, match="evaluation reported an error"):
        await client.review(
            subject=KubernetesAuthorizationSubject(username="proxy"),
            attributes=RequestAttributes.model_validate(REQUEST["attributes"]),
        )


@pytest.mark.asyncio
async def test_service_fails_closed_when_sar_times_out() -> None:
    class HangingSar(FakeSarClient):
        async def review(self, *, subject, attributes):
            await asyncio.sleep(10)
            return SubjectAccessReviewResult(allowed=True)

    service = KubernetesAuthorizationService(
        config=KubernetesAuthorizationConfig(
            subjects_by_access_profile={"public-diagnostics": KubernetesAuthorizationSubject(username="proxy")},
            timeout_seconds=0.001,
        ),
        agent_bearer_authority=cast(Any, FakeAgentBearerAuthority()),
        grants=AsyncMock(),
        sar_client=HangingSar(),
    )
    with pytest.raises(KubernetesAuthorizationUnavailableError, match="timed out"):
        await service.authorize(bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST))


@pytest.mark.asyncio
async def test_active_grant_is_consulted_only_after_clean_sar_denial() -> None:
    grants = AsyncMock()
    grants.match_request.return_value = GrantDecision(
        allowed=True,
        grant_id=UUID("00000000-0000-4000-8000-000000000099"),
        expires_at=datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC),
        reason="temporary grant",
    )
    result = await _service(
        FakeSarClient(result=SubjectAccessReviewResult(allowed=False, reason="RBAC denied")), grants=grants
    ).authorize(bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST))
    assert result.allowed is True
    assert result.source is KubernetesAuthorizationSource.GRANT
    assert result.decision_id == "grant:00000000-0000-4000-8000-000000000099"
    assert result.valid_until == datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC)
    grants.match_request.assert_awaited_once()
    kwargs = grants.match_request.await_args.kwargs
    assert kwargs["request_principal"] == RequestPrincipal(
        agent_id=_agent().agent_id, session_id=None, access_profile_id="public-diagnostics"
    )
    assert kwargs["required_scope"].kind is GrantScopeKind.NAMESPACES
    assert kwargs["required_scope"].namespaces == {"demo"}


@pytest.mark.asyncio
async def test_session_bearer_passes_exact_session_to_grant_matching() -> None:
    session_id = UUID("00000000-0000-4000-8000-000000000004")
    actor = AgentActor(
        agent_id=_agent().agent_id,
        operator_id=_agent().operator_id,
        binding_id=_agent().binding_id,
        access_profile_id="public-diagnostics",
        session_id=session_id,
    )
    grants = AsyncMock()
    grants.match_request.return_value = GrantDecision(allowed=False)
    await _service(
        FakeSarClient(result=SubjectAccessReviewResult(allowed=False, reason="RBAC denied")),
        bearer_authority=FakeAgentBearerAuthority(actor),
        grants=grants,
    ).authorize(bearer="Bearer session-token", request=AuthorizationRequest.model_validate(REQUEST))

    assert grants.match_request.await_args.kwargs["request_principal"] == RequestPrincipal(
        agent_id=actor.agent_id, session_id=session_id, access_profile_id="public-diagnostics"
    )


@pytest.mark.asyncio
async def test_sar_allow_does_not_consult_grants() -> None:
    grants = AsyncMock()
    result = await _service(FakeSarClient(result=SubjectAccessReviewResult(allowed=True)), grants=grants).authorize(
        bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST)
    )
    assert result.allowed is True
    assert result.source is KubernetesAuthorizationSource.SAR
    assert result.decision_id.startswith("sar:")
    grants.match_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_sar_outage_does_not_fall_back_to_a_matching_grant() -> None:
    grants = AsyncMock()
    grants.match_request.return_value = GrantDecision(allowed=True)
    service = _service(FakeSarClient(error=KubernetesAuthorizationUnavailableError("SAR unavailable")), grants=grants)
    with pytest.raises(KubernetesAuthorizationUnavailableError):
        await service.authorize(bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST))
    grants.match_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_authority_failure_after_sar_denial_fails_closed() -> None:
    grants = AsyncMock()
    grants.match_request.side_effect = RuntimeError("database unavailable")
    service = _service(FakeSarClient(result=SubjectAccessReviewResult(allowed=False)), grants=grants)
    with pytest.raises(KubernetesAuthorizationUnavailableError, match="grant authority"):
        await service.authorize(bearer="Bearer caller-token", request=AuthorizationRequest.model_validate(REQUEST))


if __name__ == "__main__":
    pytest_bazel.main()
