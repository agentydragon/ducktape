"""OIDC operator -> BFF -> canonical Action Service -> real MCP, with one durable dispatch."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import jwt
import pytest
import pytest_bazel
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from util.net import bind_free_port, pick_free_port
from util.testing.asgi import serve_app
from util.testing.mock_oidc import build_mock_oidc_app, generate_rsa_keypair, sign_jwt
from x.agentplane.action_service import api as service_api
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup, ActionIdentity, McpExecutorBinding
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    GrantBinding,
    GrantStatus,
    Identity,
    NewConnection,
)
from x.agentplane.action_service.database_migrate import apply_migrations
from x.agentplane.action_service.db import ActionStore, make_engine, make_sessionmaker
from x.agentplane.action_service.enrollments import ConfirmedReconnectConnection, EnrollmentAuthority, EnrollmentInput
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionState,
    Principal,
    PrincipalRole,
)
from x.agentplane.action_service.operator_oidc import OidcOperatorAuthenticator, OperatorOidcSettings
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.app.action_federation import ExchangeFederationSettings, FederatedOperatorActions
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.conftest import AGENT_AUTH
from x.agentplane.app.consent import ConsentAllow
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.oidc import OIDCSettings
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

CALLER = Principal(issuer="test-workload", subject="test-sandbox", role=PrincipalRole.CALLER)
SUBJECT_A = "test-operator-subject"
SUBJECT_B = "test-second-subject"


@dataclass
class Review:
    browser: httpx.AsyncClient
    service: ActionService
    calls: list[str]
    second_browser: httpx.AsyncClient
    login_as: Callable[[str], None]
    issuer: str
    exchanged_subjects: list[str]
    enrollments: EnrollmentAuthority
    connections: ConnectionAuthority


@pytest.fixture
def operator_connection() -> str:
    return "configured"


@pytest.fixture
async def review(
    db_url: str,
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
    operator_connection: str,
) -> AsyncIterator[Review]:
    apply_migrations(db_url)
    server = FastMCP("test-review")
    calls: list[str] = []

    @server.tool
    def record(message: str) -> dict[str, str]:
        calls.append(message)
        return {"recorded": message}

    group = ActionGroup(
        title="Test review", description="Test-only MCP tool", executor=McpExecutorBinding(description="in-memory MCP")
    )
    catalog = ActionCatalog(groups={"test_review": group})
    async with AsyncExitStack() as stack:
        engine = make_engine(db_url)
        stack.push_async_callback(engine.dispose)
        executor = McpActionGroupExecutor("test_review", group, server)
        stack.push_async_callback(executor.close)
        await executor.start()
        service = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {"test_review": executor})
        stack.push_async_callback(service.close)
        await service.start()
        private_key, public_key = generate_rsa_keypair()
        idp_sock = bind_free_port()
        idp_origin, app_url = f"http://127.0.0.1:{idp_sock.getsockname()[1]}", "http://test-app.invalid"
        idp_url = f"{idp_origin}/application/o/login/"
        target_issuer = f"{idp_origin}/application/o/actions/"
        target = OperatorOidcSettings(issuer=target_issuer, audience="test-actions", jwks_uri=f"{idp_url}jwks/")
        connections = ConnectionAuthority(
            make_sessionmaker(engine), {"public_coder": Identity(), "disabled": Identity(enabled=False)}
        )
        enrollments = EnrollmentAuthority(make_sessionmaker(engine), connections)
        downstream = service_api.create_app(
            service,
            cast(SandboxPrincipalAuthenticator, None),
            OidcOperatorAuthenticator(target),
            catalog,
            connections=connections,
            enrollments=enrollments,
            updates=ActionUpdates(db_url),
        )
        downstream_http = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=downstream), base_url="http://test-actions.invalid")
        )
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
        federation = ExchangeFederationSettings(
            service_url="http://test-actions.invalid",
            token_endpoint=f"http://127.0.0.1:{pick_free_port()}/exchange"
            if operator_connection == "exchange-disconnected"
            else f"{idp_origin}/exchange",
            login_jwks_uri=f"{idp_origin}/federation-keys?private=test-private-query"
            if operator_connection in {"jwks-unavailable", "jwks-malformed"}
            else f"{idp_url}jwks/",
            target=target,
            scope="openid profile",
        )
        operator_client = (
            None if operator_connection == "disabled" else FederatedOperatorActions(federation, oidc, downstream_http)
        )
        app = create_app(
            inventory,
            bridge,
            store,
            {provider: ["test-model"] for provider in Provider},
            egress,
            decisions,
            live_index,
            oidc,
            reviewer,
            operator_actions=operator_client,
        )
        await stack.enter_async_context(serve_app(idp, sock=idp_sock))
        browser = await stack.enter_async_context(
            httpx.AsyncClient(base_url=app_url, follow_redirects=True, mounts={app_url: httpx.ASGITransport(app=app)})
        )
        browser.headers["Origin"] = app_url
        # A distinct app/store/connection pool, sharing only PostgreSQL and cookie configuration.
        replica_store = TrajectoryStore.connect(db_url)
        stack.push_async_callback(replica_store.close)
        await replica_store.ensure_schema()
        replica = create_app(
            inventory,
            bridge,
            replica_store,
            {provider: ["test-model"] for provider in Provider},
            egress,
            decisions,
            live_index,
            oidc,
            reviewer,
            operator_actions=None
            if operator_connection == "disabled"
            else FederatedOperatorActions(federation, oidc, downstream_http),
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
            service,
            calls,
            second_browser,
            login_as,
            target_issuer,
            exchanged_subjects,
            enrollments,
            connections,
        )


async def test_connection_management_preserves_federation_csrf_versions_and_history(review: Review) -> None:
    grant = await review.connections.bind(
        GrantBinding(
            grant_id=uuid4(),
            identity_id="public_coder",
            issuer="https://test-actions.invalid",
            client_id="test-external-client",
            activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
            connection=NewConnection(display_name="Original"),
        )
    )
    await review.connections.activate(grant.id)
    original = await review.connections.get(grant.connection_id)
    path = f"/connections/{original.id}"
    browser = review.browser
    for endpoint in ["/connections", "/connection-identities", path]:
        assert (await browser.get(endpoint)).status_code == 401
        assert (await browser.get(endpoint, headers=AGENT_AUTH)).status_code == 403
    for method, endpoint, body in [
        ("PATCH", path, {"display_name": "Renamed", "expected_version": original.version}),
        ("POST", f"{path}/unbind", {"expected_version": original.version}),
    ]:
        assert (await browser.request(method, endpoint, json=body)).status_code == 401
        assert (await browser.request(method, endpoint, json=body, headers=AGENT_AUTH)).status_code == 403
    await browser.get("/auth/login")
    assert (await browser.get("/connection-identities")).json() == {
        "public_coder": {"enabled": True},
        "disabled": {"enabled": False},
    }
    assert (await browser.get("/connections")).json() == [original.model_dump(mode="json")]
    assert (await browser.get(path)).json() == original.model_dump(mode="json")
    rename = {"display_name": " Renamed ", "expected_version": original.version}
    for origin in ["https://cross-origin.invalid", "http://test-app.invalid.evil.example"]:
        assert (await browser.patch(path, json=rename, headers={"Origin": origin})).status_code == 403
        assert (
            await browser.post(
                f"{path}/unbind", json={"expected_version": original.version}, headers={"Origin": origin}
            )
        ).status_code == 403
    saved_origin = browser.headers.pop("Origin")
    assert (await browser.patch(path, json=rename)).status_code == 403
    assert (await browser.post(f"{path}/unbind", json={"expected_version": original.version})).status_code == 403
    browser.headers["Origin"] = saved_origin
    assert await review.connections.get(original.id) == original
    assert (await browser.patch(path, json={**rename, "identity_id": "disabled"})).status_code == 422
    assert (await browser.patch(path, json={**rename, "expected_version": 0})).status_code == 422
    renamed = await browser.patch(path, json=rename)
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Renamed"
    assert renamed.json()["id"] == str(original.id)
    assert renamed.json()["grants"] == original.model_dump(mode="json")["grants"]
    assert (await browser.patch(path, json=rename)).status_code == 409
    assert (await browser.post(f"{path}/unbind", json={"expected_version": original.version})).status_code == 409
    assert (await review.connections.get(original.id)).grants[0].status is GrantStatus.ACTIVE
    unbound = await browser.post(f"{path}/unbind", json={"expected_version": renamed.json()["version"]})
    assert unbound.status_code == 200
    assert unbound.json()["grants"][0]["status"] == "revoked"
    assert unbound.json()["grants"][0]["client_id"] == grant.client_id
    assert unbound.json()["grants"][0]["id"] == str(grant.id)
    assert len((await browser.get("/connections")).json()) == 1
    assert review.calls == []


@pytest.mark.parametrize("operator_connection", ["disabled", "target-subject-mismatch", "wrong-audience"])
async def test_connection_management_fails_closed_without_valid_federation(
    review: Review, operator_connection: str
) -> None:
    await review.browser.get("/auth/login")
    expected = 503 if operator_connection == "disabled" else 403
    assert (await review.browser.get("/connections")).status_code == expected
    assert (await review.browser.get("/connection-identities")).status_code == expected


async def test_operator_decision_reaches_canonical_service_and_mcp_once(review: Review) -> None:
    browser, service = review.browser, review.service
    pending = await service.submit(
        ActionRequestInput(
            idempotency_key="test-submit",
            action=ActionIdentity(group="test_review", name="record"),
            arguments={"message": "hi"},
        ),
        CALLER,
    )
    path = f"/actions/{pending.id}/decision"
    decision = {
        "verdict": "allow",
        "expected_version": pending.version,
        "idempotency_key": "test-allow",
        "decision_note": "Reviewed scope — allowed for this request.",
    }
    events_path = f"/actions/{pending.id}/events"
    assert (await browser.get(events_path)).status_code == 401
    assert (await browser.get(events_path, headers=AGENT_AUTH)).status_code == 403
    assert (await browser.get("/actions")).status_code == 401
    assert (await browser.get("/actions", headers=AGENT_AUTH)).status_code == 403
    assert (await browser.post(path, headers=AGENT_AUTH, json=decision)).status_code == 403
    assert review.calls == []

    await browser.get("/auth/login")
    assert (await browser.get("/auth/me")).json() == {"username": "test-operator"}
    assert (await browser.get("/actions", params={"state": "succeeded"})).json() == []
    assert [row["id"] for row in (await browser.get("/actions", params={"state": "decision_pending"})).json()] == [
        str(pending.id)
    ]
    assert (
        await browser.post(path, json=decision, headers={"Origin": "https://cross-origin.invalid"})
    ).status_code == 403
    assert (await service.get(pending.id, CALLER)).state is ActionState.DECISION_PENDING
    assert review.calls == []
    allowed = await browser.post(path, json=decision)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["decision"]["issuer"] == f"{review.issuer}:{SUBJECT_A}"
    assert (await browser.post(path, json={**decision, "decision_note": "ignored replay"})).json()[
        "decision"
    ] == allowed.json()["decision"]
    assert (
        await browser.post(path, json={**decision, "verdict": "deny", "idempotency_key": "late-deny"})
    ).status_code == 409
    async with asyncio.timeout(10):
        while True:
            final = (await browser.get(f"/actions/{pending.id}")).json()
            if final["state"] == "succeeded":
                break
            # Each read awaits service IO; no fixed delay or elapsed-time assertion.
    assert final["execution"]["result"] == {"recorded": "hi"}
    caller_view = await service.get(pending.id, CALLER)
    assert caller_view.decision is not None
    assert final["decision"] == caller_view.decision.model_dump(mode="json")
    assert final["decision"]["decision_note"] == decision["decision_note"]
    assert (await browser.get("/actions")).json()[0]["decision"] == final["decision"]
    assert review.calls == ["hi"]
    canonical_events = await service.events(pending.id, CALLER)
    response = await browser.get(events_path)
    assert response.status_code == 200
    assert [ActionEventView.model_validate(event) for event in response.json()] == canonical_events
    assert [event.sequence for event in canonical_events] == list(range(1, len(canonical_events) + 1))
    for cursor in (0, 2, canonical_events[-1].sequence, canonical_events[-1].sequence + 1):
        response = await browser.get(events_path, params={"after_sequence": cursor})
        assert response.status_code == 200
        assert [ActionEventView.model_validate(event) for event in response.json()] == await service.events(
            pending.id, CALLER, after_sequence=cursor
        )
    assert (await browser.get(events_path, params={"after_sequence": -1})).status_code == 422
    assert [event.state for event in canonical_events] == [
        ActionState.DECISION_PENDING,
        ActionState.ALLOWED,
        ActionState.DISPATCHING,
        ActionState.RUNNING,
        ActionState.SUCCEEDED,
    ]
    assert (await browser.post(path, json=decision)).json()["execution"]["id"] == final["execution"]["id"]
    assert review.calls == ["hi"]


@pytest.mark.parametrize(
    ("operator_connection", "expected"),
    [
        ("disabled", 503),
        ("target-subject-mismatch", 403),
        ("wrong-issuer", 403),
        ("wrong-audience", 403),
        ("expired", 403),
        ("swapped-operator", 403),
        ("source-mismatch", 403),
        ("exchange-rejected", 400),
    ],
)
async def test_unconfigured_or_rejected_service_auth_fails_closed(review: Review, expected: int) -> None:
    pending = await review.service.submit(
        ActionRequestInput(
            idempotency_key="test-blocked",
            action=ActionIdentity(group="test_review", name="record"),
            arguments={"message": "no"},
        ),
        CALLER,
    )
    await review.browser.get("/auth/login")
    for read_path in ("/actions", f"/actions/{pending.id}/events"):
        response = await review.browser.get(read_path)
        assert response.status_code == expected
        assert "test-private-provider-detail" not in response.text
        assert "access_token" not in response.text
        assert SUBJECT_A not in response.text
        assert review.issuer not in response.text
    assert (
        await review.browser.post(
            f"/actions/{pending.id}/decision",
            json={"verdict": "allow", "expected_version": pending.version, "idempotency_key": "test-blocked-allow"},
        )
    ).status_code == expected
    assert (await review.service.get(pending.id, CALLER)).state is ActionState.DECISION_PENDING
    assert review.calls == []


@pytest.mark.parametrize(
    ("operator_connection", "expected", "upstream_path", "error_type"),
    [
        ("exchange-unavailable", 503, "/exchange", "HTTPStatusError"),
        ("exchange-disconnected", 503, "/exchange", "ConnectError"),
        ("jwks-unavailable", 503, "/federation-keys", "HTTPStatusError"),
        ("exchange-malformed", 502, None, None),
        ("jwks-malformed", 503, None, None),
    ],
)
async def test_provider_availability_is_not_operator_rejection(
    review: Review, expected: int, upstream_path: str | None, error_type: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    await review.browser.get("/auth/login")
    caplog.set_level(logging.WARNING, logger="x.agentplane.app.action_federation")
    for path in ("/actions", "/mcp-servers", "/push/config"):
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
    federation_warnings = [r for r in caplog.records if r.name == "x.agentplane.app.action_federation"]
    assert len(federation_warnings) == 3
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
        pending = await review.service.submit(
            ActionRequestInput(
                idempotency_key=f"submit-{identity}-{len(review.exchanged_subjects)}",
                action=ActionIdentity(group="test_review", name="record"),
                arguments={"message": "not run"},
            ),
            CALLER,
        )
        denied = await browser.post(
            f"/actions/{pending.id}/decision",
            json={"verdict": "deny", "expected_version": pending.version, "idempotency_key": f"deny-{pending.id}"},
        )
        assert denied.status_code == 200
        assert denied.json()["decision"]["issuer"] == f"{review.issuer}:{identity}"
    assert review.exchanged_subjects == [SUBJECT_A, SUBJECT_B, SUBJECT_A]
    assert review.calls == []
    b.cookies.clear()
    b.cookies.update(old_a_cookies)
    assert (await b.post("/auth/logout")).url.path == "/"
    a.cookies.clear()
    a.cookies.update(old_a_cookies)
    assert (await a.get("/auth/me")).status_code == 401
    assert (await a.get("/actions")).status_code == 401


async def test_concurrent_cross_replica_callbacks_consume_pending_login_once(review: Review) -> None:
    a, b = review.browser, review.second_browser
    login = await a.get("/auth/login", follow_redirects=False)
    b.cookies.update(a.cookies)
    authorization = await a.get(login.headers["location"], follow_redirects=False)
    callback = authorization.headers["location"]
    responses = await asyncio.gather(a.get(callback, follow_redirects=False), b.get(callback, follow_redirects=False))
    assert sorted(response.status_code for response in responses) == [303, 401]
    assert sorted([(await a.get("/auth/me")).status_code, (await b.get("/auth/me")).status_code]) == [200, 401]


async def enrollment_path(review: Review, client_id: str = "test-external-client") -> str:
    created = await review.enrollments.create(
        EnrollmentInput(
            issuer="https://actions.test",
            client_id=client_id,
            client_name="Claude on wyrm2",
            redirect_uri="https://external-client.test/callback",
            code_challenge=f"test-pkce-{client_id}",
            upstream_url="https://authentik.test/authorize?state=server-held-state",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    return f"/connection-enrollments/{created.handle}"


async def test_consent_requires_operator_same_origin_and_preview_csrf(review: Review) -> None:
    path = await enrollment_path(review)
    browser = review.browser
    assert (await browser.post(f"{path}/preview")).status_code == 401
    assert (await browser.post(f"{path}/preview", headers=AGENT_AUTH)).status_code == 403
    await browser.get("/auth/login")
    assert (await browser.post(f"{path}/preview", headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await browser.post(f"{path}/preview", headers={"Origin": "null"})).status_code == 403
    body = {"verdict": "deny", "csrf_token": "never-previewed"}
    assert (await browser.post(f"{path}/decision", json=body)).status_code == 403
    response = await browser.post(f"{path}/preview")
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["enrollment"]["client_id"] == "test-external-client"
    assert preview["enrollment"]["redirect_uri"] == "https://external-client.test/callback"
    assert preview["identities"] == {"public_coder": {"enabled": True}, "disabled": {"enabled": False}}
    assert preview["attempted_decision"] is None
    assert "browser_binding" not in response.text
    assert "access_token" not in response.text
    assert "server-held-state" not in response.text
    assert (await browser.post(f"{path}/decision", json=body)).status_code == 403
    body["csrf_token"] = preview["csrf_token"]
    assert (
        await browser.post(f"{path}/decision", headers={"Origin": "https://evil.test"}, json=body)
    ).status_code == 403
    denied = await browser.post(f"{path}/decision", json=body)
    assert denied.status_code == 200, denied.text
    assert denied.json() == {"verdict": "deny", "redirect_url": None}
    assert (await browser.post(f"{path}/decision", json=body)).json() == denied.json()


async def test_consent_allow_round_trip_replays_across_app_replicas(review: Review) -> None:
    path = await enrollment_path(review)
    browser = review.browser
    await browser.get("/auth/login")
    preview = (await browser.post(f"{path}/preview")).json()
    body = {
        "verdict": "allow",
        "csrf_token": preview["csrf_token"],
        "connection": {"kind": "new", "display_name": "My Claude on wyrm2"},
        "identity_id": "public_coder",
    }
    result = await browser.post(f"{path}/decision", json=body)
    assert result.status_code == 200, result.text
    assert result.json() == {
        "verdict": "allow",
        "redirect_url": "https://authentik.test/authorize?state=server-held-state",
    }
    replica = review.second_browser
    replica.cookies.update(browser.cookies)
    reloaded = await replica.post(f"{path}/preview")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["attempted_decision"] == body
    assert reloaded.json()["csrf_token"] == preview["csrf_token"]
    assert (await replica.post(f"{path}/decision", json=body)).json() == result.json()
    changed = {**body, "connection": {"kind": "new", "display_name": "different name"}}
    assert (await replica.post(f"{path}/decision", json=changed)).status_code == 409
    assert (
        await replica.post(f"{path}/decision", json={"verdict": "deny", "csrf_token": body["csrf_token"]})
    ).status_code == 409
    # The BFF does not activate a grant. The OAuth adapter must still verify the same upstream operator.
    approved = await review.enrollments.approved(
        client_id="test-external-client",
        redirect_uri="https://external-client.test/callback",
        code_challenge="test-pkce-test-external-client",
        operator=Principal(issuer=review.issuer, subject=SUBJECT_A, role=PrincipalRole.OPERATOR),
    )
    assert approved.identity_id == "public_coder"
    assert review.exchanged_subjects
    assert set(review.exchanged_subjects) == {SUBJECT_A}


async def test_consent_browser_binding_survives_replica_but_not_another_login(review: Review) -> None:
    path = await enrollment_path(review)
    a, b = review.browser, review.second_browser
    await a.get("/auth/login")
    preview = (await a.post(f"{path}/preview")).json()
    await b.get("/auth/login")
    assert (await b.post(f"{path}/preview")).status_code == 403
    assert (
        await b.post(f"{path}/decision", json={"verdict": "deny", "csrf_token": preview["csrf_token"]})
    ).status_code == 403
    await a.post("/auth/logout")
    await a.get("/auth/login")
    assert (await a.post(f"{path}/preview")).status_code == 403


async def test_existing_connection_consent_requires_confirmation_and_preserves_reviewed_version(review: Review) -> None:
    old = await review.connections.bind(
        GrantBinding(
            grant_id=uuid4(),
            identity_id="public_coder",
            issuer="https://actions.test",
            client_id="test-old-client",
            activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
            connection=NewConnection(display_name="Existing test client"),
        )
    )
    old = await review.connections.activate(old.id)
    connection = await review.connections.get(old.connection_id)
    path = await enrollment_path(review)
    browser = review.browser
    await browser.get("/auth/login")
    preview = (await browser.post(f"{path}/preview")).json()
    assert preview["connections"] == [connection.model_dump(mode="json")]
    body = ConsentAllow(
        verdict="allow",
        csrf_token=preview["csrf_token"],
        identity_id="public_coder",
        connection=ConfirmedReconnectConnection(
            connection_id=connection.id, expected_version=connection.version, authority_change_confirmed=True
        ),
    ).model_dump(mode="json")
    body["connection"].pop("authority_change_confirmed")
    assert (await browser.post(f"{path}/decision", json=body)).status_code == 422
    body["connection"]["authority_change_confirmed"] = True
    assert (
        await browser.post(f"{path}/decision", json=body, headers={"Origin": "https://evil.test"})
    ).status_code == 403
    result = await browser.post(f"{path}/decision", json=body)
    assert result.status_code == 200, result.text
    assert await review.connections.resolve(old.id, issuer=old.issuer, client_id=old.client_id) == old
    renamed = await review.connections.rename(
        connection.id, expected_version=connection.version, display_name="Changed after review"
    )
    replica = review.second_browser
    replica.cookies.update(browser.cookies)
    reloaded = (await replica.post(f"{path}/preview")).json()
    assert reloaded["attempted_decision"] == body
    assert reloaded["connections"][0]["version"] == renamed.version
    assert (await replica.post(f"{path}/decision", json=body)).json() == result.json()
    changed = {**body, "connection": {**body["connection"], "expected_version": renamed.version}}
    assert (await replica.post(f"{path}/decision", json=changed)).status_code == 409


async def test_consent_interactions_have_distinct_csrf_and_reject_extra_authority(review: Review) -> None:
    first, second = await enrollment_path(review, "one"), await enrollment_path(review, "two")
    browser = review.browser
    await browser.get("/auth/login")
    previews = await asyncio.gather(browser.post(f"{first}/preview"), browser.post(f"{second}/preview"))
    one, two = [response.json() for response in previews]
    assert one["csrf_token"] != two["csrf_token"]
    assert (
        await browser.post(f"{second}/decision", json={"verdict": "deny", "csrf_token": one["csrf_token"]})
    ).status_code == 403
    for extra in (
        {"redirect_url": "https://evil.test"},
        {"browser_binding": "attacker-supplied"},
        {"identity_id": "public_coder"},
    ):
        response = await browser.post(
            f"{first}/decision", json={"verdict": "deny", "csrf_token": one["csrf_token"], **extra}
        )
        assert response.status_code == 422, response.text
    denied = await browser.post(f"{first}/decision", json={"verdict": "deny", "csrf_token": one["csrf_token"]})
    assert denied.status_code == 200
    assert (await browser.post(f"{second}/preview")).json()["attempted_decision"] is None


if __name__ == "__main__":
    pytest_bazel.main()
