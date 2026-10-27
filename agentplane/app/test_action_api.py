"""OIDC operator -> BFF -> canonical Action Service -> real MCP, with one durable dispatch."""

from __future__ import annotations

# Generated service annotations need the grpcio typing dependency.
# gazelle:include_dep @pypi//grpcio
# Generated protobuf annotations need the protobuf typing dependency.
# gazelle:include_dep @pypi//protobuf
import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import pytest_bazel
from fastapi import FastAPI
from pydantic import AnyHttpUrl
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentplane.action_service import action_service_pb2, action_service_pb2_grpc
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_federation_settings import DirectFederationSettings, ExchangeFederationSettings
from agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
from agentplane.app.api import CallerGrantView, create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex, SandboxSnapshot
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE, OIDCSettings
from agentplane.app.operator_sessions import BrowserSession, OperatorSession, OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.fake_action_service import ActionCall, FakeActionService
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.subjects import ServiceAccountRef
from util.testing.asgi import bind_http_server, serve_app_in_loop
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair, sign_jwt

SUBJECT_A = "test-operator-subject"
SUBJECT_B = "test-second-subject"


@dataclass
class Review:
    browser: httpx.AsyncClient
    second_browser: httpx.AsyncClient
    login_as: Callable[[str], None]
    issuer: str
    exchanged_subjects: list[str]
    app: FastAPI
    # Every refresh token presented to the login provider, in order.
    refreshes: list[str]
    verification_key: Any
    audience: str
    action_calls: list[ActionCall]


@pytest.fixture
def operator_connection() -> str:
    return "configured"


@pytest.fixture
def direct_federation() -> bool:
    return False


@pytest.fixture
async def review(
    db_url: str,
    inventory: SandboxAccess,
    gateway: SandboxControlClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    operator_connection: str,
    refused_address: str,
    direct_federation: bool,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    history_client: HistoryServiceClient,
    electric_proxy: ElectricProxy,
    notifications: NotificationsOperatorClient,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> AsyncIterator[Review]:
    """Exercise the app's ASGI and gRPC client behavior against an in-process Action Service."""
    async with AsyncExitStack() as stack:
        private_key, public_key = generate_rsa_keypair()
        idp_endpoint = stack.enter_context(bind_http_server())
        idp_origin, app_url = idp_endpoint.origin, "http://test-app.invalid"
        idp_url = f"{idp_origin}/application/o/login/"
        target_issuer = idp_url if direct_federation else f"{idp_origin}/application/o/actions/"
        target = OperatorOidcSettings(
            issuer=target_issuer,
            audience="test-app" if direct_federation and operator_connection != "wrong-audience" else "test-actions",
            jwks_uri=f"{idp_url}jwks/",
        )
        fake_actions, action_stub, operator_grpc = action_service
        policy = fake_actions.policy.action_policy
        policy.synced = True
        policy.subject.namespace = "test-sandboxes"
        policy.subject.name = "live"
        effective = policy.auto_approve_if.add(
            binding="live-launch", policy_set="test-reads", source=action_service_pb2.POLICY_SOURCE_RULES, index=0
        )
        effective.policy.exact_actions.actions["test_review"].values.append("record")
        exchanged_subjects: list[str] = []

        async def exchange(request: Request) -> JSONResponse:
            body = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
            assert body["grant_type"] == "client_credentials"
            assert body["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            assert "client_secret" not in body
            claims = jwt.decode(
                body["client_assertion"], public_key, algorithms=["RS256"], issuer=idp_url, audience="test-app"
            )
            exchanged_subjects.append(claims["sub"])
            if operator_connection == "exchange-unavailable":
                return JSONResponse({"private": "test-private-provider-detail"}, status_code=503)
            if operator_connection == "exchange-malformed":
                return JSONResponse({"error": "server_error", "error_description": "test-private-provider-detail"})
            if operator_connection == "exchange-rejected":
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "test-private-provider-detail"}, status_code=400
                )
            now = int(time.time())
            subject = claims["sub"]
            if operator_connection == "target-subject-mismatch":
                subject = "unauthorized"
            exchanged = {
                "iss": target_issuer,
                "aud": "test-actions",
                "azp": "test-actions",
                "sub": subject,
                "iat": now,
                "exp": now + 60,
            }
            if operator_connection == "wrong-issuer":
                exchanged["iss"] = idp_url
            elif operator_connection == "wrong-audience":
                exchanged["aud"] = "other-service"
            elif operator_connection == "expired":
                exchanged.update(iat=now - 600, exp=now - 300)
            elif operator_connection == "swapped-operator":
                exchanged["sub"] = SUBJECT_B
            return JSONResponse(
                {"access_token": sign_jwt(private_key, exchanged), "token_type": "Bearer", "expires_in": 60}
            )

        refreshes: list[str] = []

        def login_provider(subject: str):
            provider = build_mock_oidc_app(
                issuer_url=idp_url,
                private_key=private_key,
                public_key=public_key,
                subject=subject,
                extra_id_token_claims={
                    "preferred_username": "test-operator",
                    "sub": SUBJECT_B if operator_connection == "source-mismatch" else subject,
                },
                authentik_compatible=True,
            )
            issue = next(
                route.endpoint
                for route in provider.routes
                if isinstance(route, Route) and route.path == "/application/o/token/"
            )

            async def token(request: Request) -> Response:
                form = parse_qs((await request.body()).decode())
                if form["grant_type"] == ["refresh_token"]:
                    refreshes.append(form["refresh_token"][0])
                    if operator_connection == "refresh-refused":
                        return JSONResponse(
                            {"error": "invalid_grant", "error_description": "test-private-provider-detail"},
                            status_code=400,
                        )
                # The mock renews by refresh token too, issuing a new one each time.
                return cast(Response, await issue(request))

            provider.routes.insert(0, Route("/application/o/token/", token, methods=["POST"]))
            provider.routes.insert(0, Route("/exchange", exchange, methods=["POST"]))
            return provider

        idp = login_provider(SUBJECT_A)

        async def unavailable_keys(request: Request) -> JSONResponse:
            if operator_connection == "jwks-malformed":
                return JSONResponse({"keys": [], "private": "test-private-provider-detail"})
            return JSONResponse({"private": "test-private-provider-detail"}, status_code=503)

        idp.routes.insert(0, Route("/federation-keys", unavailable_keys))

        def login_as(subject: str) -> None:
            idp.routes[:] = login_provider(subject).routes

        oidc = OIDCSettings(
            issuer=idp_url,
            client_id="test-app",
            client_secret="test-only-client-secret",
            session_secret="test-only-session-secret",
            public_base_url=app_url,
        )
        federation = (
            DirectFederationSettings(
                service_url=AnyHttpUrl("http://test-actions.invalid"),
                login_jwks_uri=f"{idp_origin}/federation-keys?private=test-private-query"
                if operator_connection in {"jwks-unavailable", "jwks-malformed"}
                else f"{idp_url}jwks/",
                target=target,
                scope="openid profile",
            )
            if direct_federation
            else ExchangeFederationSettings(
                service_url=AnyHttpUrl("http://test-actions.invalid"),
                token_endpoint=AnyHttpUrl(f"http://{refused_address}/exchange")
                if operator_connection == "exchange-disconnected"
                else AnyHttpUrl(f"{idp_origin}/exchange"),
                login_jwks_uri=f"{idp_origin}/federation-keys?private=test-private-query"
                if operator_connection in {"jwks-unavailable", "jwks-malformed"}
                else f"{idp_url}jwks/",
                target=target,
                scope="openid profile",
            )
        )
        operator_client = None if operator_connection == "disabled" else FederatedOperatorActions(federation, oidc)
        app = create_app(
            inventory,
            bridge,
            store,
            TEST_MODELS,
            egress,
            decisions,
            live_index,
            action_policy,
            oidc,
            reviewer,
            operator_actions=operator_client,
            event_logs=event_logs,
            content=content,
            database_updates=database_updates,
            operator_sessions=operator_sessions,
            electric=electric_proxy,
            notifications=notifications,
            action_service=action_stub,
            operator_grpc=operator_grpc,
            system_status=answering_system_status(),
        )
        await stack.enter_async_context(idp_endpoint.serve(idp))
        browser = await stack.enter_async_context(
            httpx.AsyncClient(base_url=app_url, follow_redirects=True, mounts={app_url: httpx.ASGITransport(app=app)})
        )
        browser.headers["Origin"] = app_url
        # A distinct app/store/connection pool, sharing only PostgreSQL and cookie configuration.
        replica_engine = connect(db_url)
        stack.push_async_callback(replica_engine.dispose)
        replica_summaries = Summaries(history_client)
        replica = create_app(
            inventory,
            bridge,
            ThreadStore(replica_engine, replica_summaries),
            TEST_MODELS,
            egress,
            decisions,
            live_index,
            action_policy,
            oidc,
            reviewer,
            operator_actions=None if operator_connection == "disabled" else FederatedOperatorActions(federation, oidc),
            event_logs=SeededEventLogStore(
                replica_engine,
                history=history,
                history_reader=history_client,
                summaries=replica_summaries,
                sandbox_namespaces=event_logs.sandbox_namespaces,
            ),
            content=ContentStore(replica_engine, history_reader=history_client),
            # Never started: nothing served here listens; the replica shares only the operator sessions.
            database_updates=create_database_updates(replica_engine.url),
            operator_sessions=OperatorSessionStore(replica_engine),
            electric=electric_proxy,
            notifications=notifications,
            action_service=action_stub,
            operator_grpc=operator_grpc,
            system_status=answering_system_status(),
        )
        second_browser = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=app_url,
                follow_redirects=True,
                headers={"Origin": app_url},
                mounts={app_url: httpx.ASGITransport(app=replica)},
            )
        )
        yield Review(
            browser,
            second_browser,
            login_as,
            target_issuer,
            exchanged_subjects,
            app,
            refreshes,
            public_key,
            target.audience,
            fake_actions.calls,
        )


@pytest.fixture
async def review_origin(review: Review) -> AsyncIterator[str]:
    """The app on a real socket, in this loop: the in-memory transport waits for a body that never
    ends, and another thread's loop could not use the store's connections."""
    async with serve_app_in_loop(review.app) as origin:
        yield origin


async def _first_snapshot(client: httpx.AsyncClient, path: str, headers: dict[str, str]) -> SandboxSnapshot:
    async with client.stream("GET", path, headers=headers) as stream:
        lines = stream.aiter_lines()
        async for line in lines:
            if line == "event: snapshot":
                return SandboxSnapshot.model_validate_json((await anext(lines)).removeprefix("data: "))
    raise AssertionError("the stream ended before its first snapshot")


def _assert_policy_grpc_call(review: Review, subject: ServiceAccountRef) -> None:
    matching = [
        call
        for call in review.action_calls
        if call.method == "GetActionPolicy"
        and isinstance(call.request, action_service_pb2.GetActionPolicyRequest)
        and call.request.subject.namespace == subject.namespace
        and call.request.subject.name == subject.name
    ]
    assert len(matching) == 1
    call = matching[0]
    assert call.bearer_token is not None
    assert call.bearer_token.startswith("Bearer ")
    claims = jwt.decode(
        call.bearer_token.removeprefix("Bearer "),
        review.verification_key,
        algorithms=["RS256"],
        issuer=review.issuer,
        audience=review.audience,
    )
    assert isinstance(claims["sub"], str)


@pytest.mark.parametrize("operator_connection", ["configured", "disabled"])
async def test_the_sandbox_policy_frame_is_the_services_answer_for_its_account_or_says_why_not(
    review: Review,
    namespace: SandboxNamespace,
    agent_auth: dict[str, str],
    live_index: LiveIndex,
    operator_connection: str,
    review_origin: str,
) -> None:
    """The live frame carries what the Action Service resolves for the ServiceAccount the sandbox
    runs as, through the operator federation. Where the service cannot be asked, the frame says so
    in place of the policy rather than showing an empty one, and an agent watching the same stream
    is told why rather than shown an operator's answer."""
    live_index.sandboxes["live"] = await namespace.sandbox("live")
    browser = review.browser
    await browser.get("/auth/login")
    session = {"Cookie": f"{INSECURE_COOKIE}={browser.cookies[INSECURE_COOKIE]}"}

    async with httpx.AsyncClient(base_url=review_origin) as client:
        frame = await _first_snapshot(client, "/live/sandboxes/live", session)
        as_agent = await _first_snapshot(client, "/live/sandboxes/live", agent_auth)

    assert frame.sandbox is not None
    if operator_connection == "disabled":
        assert frame.action_policy == ActionPolicyUnavailable(code="operator_federation_not_configured")
    else:
        view = frame.action_policy
        assert isinstance(view, ActionPolicyView)
        assert view.synced is True
        assert [(p.binding, p.policy_set, p.index) for p in view.auto_approve_if] == [("live-launch", "test-reads", 0)]
        _assert_policy_grpc_call(review, ServiceAccountRef(namespace=namespace.name, name="live"))
    assert as_agent.action_policy == ActionPolicyUnavailable(code="operator_session_required")


@pytest.mark.parametrize("operator_connection", ["configured", "disabled"])
async def test_caller_grants_without_a_sandbox(
    review: Review, namespace: SandboxNamespace, operator_connection: str
) -> None:
    for subject, name in (
        (namespace.account("live"), "external"),
        (ServiceAccountRef(namespace="other-namespace", name="live"), "unrelated"),
    ):
        await namespace.policy_binding(
            name, subject=subject, policy_sets=["missing-policy"], expires_at="2000-01-01T00:00:00Z"
        )
    # No Sandbox, OAuth Connection or caller-admission label is needed to inspect bindings.
    assert await namespace.names(SANDBOXES_PLURAL) == set()
    await review.browser.get("/auth/login")
    response = await review.browser.get(f"/caller-grants/{namespace.name}/live")
    assert response.status_code == 200
    view = CallerGrantView.model_validate(response.json())
    assert [binding.name for binding in view.bindings] == ["external"]
    assert view.bindings[0].missing_policy_sets == ["missing-policy"]
    assert view.bindings[0].expires_at == datetime(2000, 1, 1, tzinfo=UTC)
    if operator_connection == "disabled":
        assert view.action_policy == ActionPolicyUnavailable(code="operator_federation_not_configured")
    else:
        assert isinstance(view.action_policy, ActionPolicyView)
        assert [policy.binding for policy in view.action_policy.auto_approve_if] == ["live-launch"]
        _assert_policy_grpc_call(review, namespace.account("live"))
    other = await review.browser.get("/caller-grants/other-namespace/live")
    other.raise_for_status()
    assert [binding["name"] for binding in other.json()["bindings"]] == ["unrelated"]
    if operator_connection == "configured":
        _assert_policy_grpc_call(review, ServiceAccountRef(namespace="other-namespace", name="live"))


async def test_caller_grants_require_an_operator_session(
    review_origin: str, namespace: SandboxNamespace, agent_auth: dict[str, str]
) -> None:
    async with httpx.AsyncClient(base_url=review_origin) as client:
        path = f"/caller-grants/{namespace.name}/live"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=agent_auth)).status_code == 403


async def test_action_bff_builds_rpc_requests_and_rejects_invalid_proto_json(review: Review) -> None:
    await review.browser.get("/auth/login")

    listed = await review.browser.get(
        "/upstream/actions/v1/action-requests",
        params=[
            ("state", "ACTION_STATE_DECISION_PENDING"),
            ("state", "ACTION_STATE_ALLOWED"),
            ("idempotencyKey", "recover-key"),
            ("limit", "3"),
            ("afterId", "last-id"),
        ],
    )
    assert listed.status_code == 200, listed.text
    call = next(call for call in review.action_calls if call.method == "ListActionRequests")
    assert isinstance(call.request, action_service_pb2.ListActionRequestsRequest)
    assert list(call.request.state) == [
        action_service_pb2.ACTION_STATE_DECISION_PENDING,
        action_service_pb2.ACTION_STATE_ALLOWED,
    ]
    assert call.request.idempotency_key == "recover-key"
    assert call.request.limit == 3
    assert call.request.after_id == "last-id"
    assert call.bearer_token is not None
    assert call.bearer_token.startswith("Bearer ")

    invalid_state = await review.browser.get(
        "/upstream/actions/v1/action-requests", params={"state": "ACTION_STATE_UNKNOWN"}
    )
    assert invalid_state.status_code == 400

    decision = "/upstream/actions/v1/operator/action-requests/test-id/decision"
    for body in (b"{", b'{"unknownField":"value"}'):
        malformed = await review.browser.post(decision, content=body, headers={"content-type": "application/json"})
        assert malformed.status_code == 400
        assert malformed.json()["detail"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("direct_federation", "operator_connection", "expected", "upstream_path", "error_type"),
    [
        (False, "exchange-unavailable", 503, "/exchange", "HTTPStatusError"),
        (False, "exchange-disconnected", 503, "/exchange", "ConnectError"),
        (False, "jwks-unavailable", 503, "/federation-keys", "HTTPStatusError"),
        (False, "exchange-malformed", 502, None, None),
        (False, "jwks-malformed", 503, None, None),
        (True, "jwks-unavailable", 503, "/federation-keys", "HTTPStatusError"),
        (True, "jwks-malformed", 503, None, None),
    ],
    ids=[
        "exchange-unavailable",
        "exchange-disconnected",
        "exchange-login-jwks-unavailable",
        "exchange-malformed",
        "exchange-login-jwks-malformed",
        "direct-jwks-unavailable",
        "direct-jwks-malformed",
    ],
)
async def test_provider_availability_is_not_operator_rejection(
    review: Review,
    direct_federation: bool,
    expected: int,
    upstream_path: str | None,
    error_type: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await review.browser.get("/auth/login")
    caplog.set_level(logging.WARNING, logger="agentplane.app.action_federation")
    paths = ("/upstream/actions/v1/action-requests", "/upstream/actions/v1/action-groups")
    for path in paths:
        response = await review.browser.get(path)
        assert response.status_code == expected, response.text
        detail = response.json()["detail"]
        if upstream_path is not None:
            assert httpx.URL(detail["url"]).path == upstream_path
            assert detail["method"] == ("POST" if upstream_path == "/exchange" else "GET")
            assert detail["error_type"] == error_type
            assert detail["upstream_status"] == (503 if error_type == "HTTPStatusError" else None)
        else:
            assert detail["code"] in {
                "operator_federation_exchange_failed",
                "operator_federation_verification_unavailable",
            }
        assert "test-private" not in response.text
        assert "access_token" not in response.text
        assert SUBJECT_A not in response.text
    # Every failure leaves a cause in the log, and the log leaks no more than the response does.
    federation_warnings = [r for r in caplog.records if r.name == "agentplane.app.action_federation"]
    assert len(federation_warnings) == len(paths)
    assert "test-private" not in caplog.text
    assert "access_token" not in caplog.text
    assert SUBJECT_A not in caplog.text


async def test_two_replicas_share_login_callback_and_logout_and_keep_two_operators_distinct(review: Review) -> None:
    a, b = review.browser, review.second_browser
    login = await a.get("/auth/login", follow_redirects=False)
    assert login.status_code == 302
    b.cookies.update(a.cookies)
    authenticated = await b.get(login.headers["location"])
    assert authenticated.url.path == "/"
    a.cookies.update(b.cookies)
    old_a_cookies = httpx.Cookies(a.cookies)
    assert (await a.get("/auth/me")).json() == {"username": "test-operator"}
    assert (await b.get("/auth/me")).json() == {"username": "test-operator"}
    for cookie in a.cookies.jar:
        assert cookie.value is not None
        assert SUBJECT_A not in cookie.value
        assert "test-operator" not in cookie.value
        assert "access_token" not in cookie.value
        assert len(cookie.value) < 150
    b.cookies.clear()
    review.login_as(SUBJECT_B)
    await b.get("/auth/login")
    for browser, identity in ((a, SUBJECT_A), (b, SUBJECT_B), (a, SUBJECT_A)):
        assert (await browser.get("/upstream/actions/v1/action-requests")).status_code == 200
        assert review.exchanged_subjects[-1] == identity
    assert review.exchanged_subjects == [SUBJECT_A, SUBJECT_B, SUBJECT_A]
    b.cookies.clear()
    b.cookies.update(old_a_cookies)
    assert (await b.post("/auth/logout")).url.path == "/"
    a.cookies.clear()
    a.cookies.update(old_a_cookies)
    assert (await a.get("/auth/me")).status_code == 401
    assert (await a.get("/upstream/actions/v1/action-requests")).status_code == 401


async def _stored_login(sessions: OperatorSessionStore) -> tuple[BrowserSession, OperatorSession]:
    async with sessions.sessions() as db:
        row = (await db.scalars(select(BrowserSession))).one()
    login = row.login
    assert login is not None
    return row, login


async def _expire_login_token(sessions: OperatorSessionStore) -> str:
    """Age the stored access token past renewal, returning the refresh token that renews it."""
    async with sessions.sessions.begin() as db:
        row = (await db.scalars(select(BrowserSession).with_for_update())).one()
        row.access_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert row.refresh_token is not None
    return row.refresh_token


async def test_a_login_keeps_what_renews_it_and_outlasts_its_access_token(
    review: Review, operator_sessions: OperatorSessionStore
) -> None:
    login = await review.browser.get("/auth/login", follow_redirects=False)
    assert "offline_access" in httpx.URL(login.headers["location"]).params["scope"].split()
    before = datetime.now(UTC)
    await review.browser.get(login.headers["location"])
    after = datetime.now(UTC)

    row, session = await _stored_login(operator_sessions)
    assert session.tokens is not None
    assert session.tokens.refresh_token is not None
    # The provider's access token lasts an hour; the session a day of idleness, a week at most.
    assert session.tokens.expires_at <= after + timedelta(hours=1)
    assert before + timedelta(days=1) <= row.expires_at <= after + timedelta(days=1)
    assert before + timedelta(days=7) <= row.absolute_expires_at <= after + timedelta(days=7)


async def test_an_expired_login_token_is_renewed_once_for_the_exchange(
    review: Review, operator_sessions: OperatorSessionStore
) -> None:
    await review.browser.get("/auth/login")
    spent = await _expire_login_token(operator_sessions)

    assert (await review.browser.get("/upstream/actions/v1/action-requests")).status_code == 200
    assert (await review.browser.get("/upstream/actions/v1/action-requests")).status_code == 200

    _, session = await _stored_login(operator_sessions)
    assert session.tokens is not None
    assert session.tokens.refresh_token is not None
    assert review.refreshes == [spent]
    assert session.tokens.refresh_token.get_secret_value() != spent
    assert session.tokens.expires_at > datetime.now(UTC)
    assert review.exchanged_subjects == [SUBJECT_A, SUBJECT_A]


@pytest.mark.parametrize("operator_connection", ["refresh-refused"])
async def test_a_refused_renewal_ends_the_session_and_asks_for_a_login(
    review: Review, operator_sessions: OperatorSessionStore
) -> None:
    await review.browser.get("/auth/login")
    spent = await _expire_login_token(operator_sessions)

    refused = await review.browser.get("/upstream/actions/v1/action-requests")

    assert refused.status_code == 401
    assert refused.json() == {"detail": {"code": "operator_reauthentication_required"}}
    assert review.refreshes == [spent]
    async with operator_sessions.sessions() as db:
        assert (await db.scalars(select(BrowserSession))).all() == []
    assert (await review.browser.get("/auth/me")).status_code == 401
    assert review.exchanged_subjects == []


async def test_concurrent_cross_replica_callbacks_consume_pending_login_once(review: Review) -> None:
    a, b = review.browser, review.second_browser
    login = await a.get("/auth/login", follow_redirects=False)
    b.cookies.update(a.cookies)
    authorization = await a.get(login.headers["location"], follow_redirects=False)
    callback = authorization.headers["location"]
    responses = await asyncio.gather(a.get(callback, follow_redirects=False), b.get(callback, follow_redirects=False))
    assert sorted(response.status_code for response in responses) == [303, 401]
    assert sorted([(await a.get("/auth/me")).status_code, (await b.get("/auth/me")).status_code]) == [200, 401]


if __name__ == "__main__":
    pytest_bazel.main()
