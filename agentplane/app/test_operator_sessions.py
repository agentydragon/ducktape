"""Requests presenting one browser session, through `OperatorSessionMiddleware` in the app
`create_app` builds: none waits for another, what each makes of the session survives the others, and
none is stored by the browser unless its route says how.

Requests meant to overlap park at a `Gate` in their handlers -- after the middleware has read the
session, before it saves what they made of it -- or in the Action Service's answer. Each overlap is
bounded, because the regression it guards against waits for the parked request, which never comes.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from more_itertools import one
from sqlalchemy import select, update
from starlette.types import Message, Scope

from agentplane.action_service import action_service_pb2_grpc
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import app_services, create_app
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE, OIDCSettings
from agentplane.app.operator_sessions import BrowserSession, OperatorSession, OperatorSessionStore, request_session
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.fake_action_service import FakeActionService
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.history_service.testing.history import History
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# Generated service annotations need the grpcio typing dependency.
# gazelle:include_dep @pypi//grpcio

APP_URL = "http://test-app.invalid"
OIDC = OIDCSettings(
    issuer="https://test-idp.invalid/",
    client_id="test-app",
    client_secret="test-client-secret",  # a test literal, not a real credential
    session_secret="test-session-secret",  # a test literal, not a real credential
    public_base_url=APP_URL,
)
LOGIN = OperatorSession(issuer=OIDC.issuer, subject="test-subject", username="test-operator")
SHAPE_TAG = '"test-shape-tag"'


class Gate:
    """Parks every request that reaches it until it opens."""

    def __init__(self) -> None:
        self._parked: asyncio.Queue[None] = asyncio.Queue()
        self.opened = asyncio.Event()

    async def wait(self) -> None:
        self._parked.put_nowait(None)
        await self.opened.wait()

    async def reached(self, count: int = 1) -> None:
        """Return once `count` more requests are parked."""
        for _ in range(count):
            await self._parked.get()

    def open(self) -> None:
        self.opened.set()


class StubActions(FederatedOperatorActions):
    def __init__(self) -> None:
        pass

    async def token_for_request(self, request: Request) -> str:
        return "test-actions-token"


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
async def electric_http() -> AsyncIterator[httpx.AsyncClient]:
    """Electric serving every shape read under one entity tag, and 304 to a read that presents it."""

    async def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == SHAPE_TAG:
            return httpx.Response(304, stream=httpx.ByteStream(b""), headers={"etag": SHAPE_TAG})
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"), headers={"etag": SHAPE_TAG})

    async with httpx.AsyncClient(
        base_url="http://test-electric.invalid", transport=httpx.MockTransport(answer)
    ) as client:
        yield client


@pytest.fixture
async def folded(event_logs: SeededEventLogStore, history: History) -> UUID:
    """A thread whose fold gives its shapes a scope."""
    source = ReplicationSource()
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.set_attached(thread, source.attached)
    await history.record(thread, source.entries)
    return thread


@pytest.fixture
async def app(
    gate: Gate,
    inventory: SandboxAccess,
    bridge: RunnerBridge,
    store: ThreadStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    operator_sessions: OperatorSessionStore,
    database_updates: WakeupListener,
    electric_proxy: ElectricProxy,
    notifications: NotificationsOperatorClient,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> AsyncIterator[FastAPI]:
    """The app with a login that needs no identity provider, and an Action Service that answers once
    `gate` opens."""
    service, action_stub, operator_grpc = action_service
    service.unary_gate = gate.opened
    service.watch_gate = gate.opened
    app = create_app(
        inventory,
        bridge,
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        OIDC,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        notifications=notifications,
        system_status=answering_system_status(),
    )

    @app.post("/test-login")
    async def login(request: Request) -> None:
        request_session(request).log_in(LOGIN, absolute_expires_at=datetime.now(UTC) + timedelta(hours=1))

    @app.post("/test-pending")
    async def pending(request: Request) -> None:
        request.session["test-state"] = "test-pending-state"

    @app.get("/test-held")
    async def held() -> None:
        await gate.wait()

    @app.post("/test-edit")
    async def edit(request: Request, key: str, value: str | None = None) -> None:
        await gate.wait()
        if value is None:
            del request.session[key]
        else:
            request.session[key] = value

    @app.post("/test-callback")
    async def callback(request: Request) -> None:
        await gate.wait()
        request_session(request).log_in(LOGIN, absolute_expires_at=datetime.now(UTC) + timedelta(hours=1))

    app_services(app).operator_actions = StubActions()
    app_services(app).action_service = action_stub
    app_services(app).operator_grpc = operator_grpc
    try:
        yield app
    finally:
        # Nothing a failed test left parked outlives it.
        gate.open()


def _browser(app: FastAPI, cookie: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=APP_URL,
        cookies={INSECURE_COOKIE: cookie} if cookie is not None else None,
    )


async def _session(app: FastAPI, path: str) -> str:
    """The cookie of a session `path` starts."""
    async with _browser(app) as browser:
        (await browser.post(path)).raise_for_status()
        return browser.cookies[INSECURE_COOKIE]


async def _rows(sessions: OperatorSessionStore) -> list[BrowserSession]:
    async with sessions.sessions() as db:
        return list(await db.scalars(select(BrowserSession)))


class RawRequest:
    """A GET driven straight through ASGI: httpx's transport would buffer a body that never ends."""

    def __init__(self, app: FastAPI, path: str, cookie: str) -> None:
        self.started = asyncio.Event()
        self._disconnected = asyncio.Event()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": [(b"cookie", f"{INSECURE_COOKIE}={cookie}".encode())],
        }
        self._served = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.started.set()

    async def hang_up(self) -> None:
        """Once the response has started, go away and let the app finish."""
        async with asyncio.timeout(10):
            await self.started.wait()
            self._disconnected.set()
            await self._served


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/upstream/actions/v1/action-requests", id="action-service-answer"),
        pytest.param("/upstream/actions/v1/action-requests:watch", id="action-stream-open"),
        pytest.param("/test-held", id="slow-handler"),
    ],
)
async def test_a_request_held_before_its_headers_holds_up_no_other_on_its_session(
    app: FastAPI,
    gate: Gate,
    path: str,
    action_service: tuple[FakeActionService, action_service_pb2_grpc.ActionServiceStub, OperatorGrpcClient],
) -> None:
    cookie = await _session(app, "/test-login")
    held = RawRequest(app, path, cookie)
    async with asyncio.timeout(10):
        if path == "/upstream/actions/v1/action-requests":
            await action_service[0].unary_started.wait()
        elif path == "/upstream/actions/v1/action-requests:watch":
            await action_service[0].watch_started.wait()
        else:
            await gate.reached()
        async with _browser(app, cookie) as browser:
            assert (await browser.get("/models")).status_code == 200
    assert not held.started.is_set()
    gate.open()
    await held.hang_up()


async def test_a_request_waits_on_no_renewal_holding_its_row(
    app: FastAPI, operator_sessions: OperatorSessionStore
) -> None:
    """A renewal holds its session's row across its call to the identity provider. A request
    meanwhile neither waits for it nor fails, leaving the idle deadline, due to move, to the next."""
    cookie = await _session(app, "/test-login")
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    async with operator_sessions.sessions.begin() as db:
        await db.execute(update(BrowserSession).values(expires_at=deadline))

    async with _browser(app, cookie) as browser:
        async with operator_sessions.sessions.begin() as renewal:
            await renewal.scalar(select(BrowserSession).with_for_update())
            async with asyncio.timeout(10):
                assert (await browser.get("/models")).status_code == 200
            assert [row.expires_at for row in await _rows(operator_sessions)] == [deadline]
        assert (await browser.get("/models")).status_code == 200
    assert [row.expires_at > deadline for row in await _rows(operator_sessions)] == [True]


async def test_concurrent_requests_each_keep_their_change_to_the_session(
    app: FastAPI, gate: Gate, operator_sessions: OperatorSessionStore
) -> None:
    """Both read the payload before either saves; each change applies to it as the other left it."""
    cookie = await _session(app, "/test-login")
    async with operator_sessions.sessions.begin() as db:
        await db.execute(update(BrowserSession).values(payload={"test-kept": "before", "test-dropped": "before"}))

    async with _browser(app, cookie) as browser, asyncio.timeout(10):
        edits = [
            asyncio.create_task(browser.post("/test-edit", params=params))
            for params in ({"key": "test-added", "value": "after"}, {"key": "test-dropped"})
        ]
        await gate.reached(2)
        gate.open()
        assert [(await edit).status_code for edit in edits] == [200, 200]
    assert [row.payload for row in await _rows(operator_sessions)] == [{"test-kept": "before", "test-added": "after"}]


async def test_a_change_to_a_session_logged_out_meanwhile_does_not_bring_it_back(
    app: FastAPI, gate: Gate, operator_sessions: OperatorSessionStore
) -> None:
    cookie = await _session(app, "/test-login")

    async with _browser(app, cookie) as browser, asyncio.timeout(10):
        edit = asyncio.create_task(browser.post("/test-edit", params={"key": "test-added", "value": "after"}))
        await gate.reached()
        assert (await browser.post("/auth/logout", headers={"Origin": APP_URL})).status_code == 303
        gate.open()
        saved = await edit
    assert saved.status_code == 200
    assert "Max-Age=0" in saved.headers["set-cookie"]
    assert await _rows(operator_sessions) == []
    async with _browser(app, cookie) as browser:
        assert (await browser.get("/auth/me")).status_code == 401


async def test_a_pending_login_is_completed_once(
    app: FastAPI, gate: Gate, operator_sessions: OperatorSessionStore
) -> None:
    """Two callbacks read one pending login before either completes it: one does, and the other is
    refused and creates nothing."""
    pending = await _session(app, "/test-pending")

    async with _browser(app, pending) as first, _browser(app, pending) as second, asyncio.timeout(10):
        callbacks = [asyncio.create_task(browser.post("/test-callback")) for browser in (first, second)]
        await gate.reached(2)
        gate.open()
        responses = [await callback for callback in callbacks]
    completed = one(response for response in responses if response.status_code == 200)
    refused = one(response for response in responses if response.status_code == 401)
    assert "Max-Age=0" in refused.headers["set-cookie"]
    assert [row.login for row in await _rows(operator_sessions)] == [LOGIN]
    async with _browser(app, completed.cookies[INSECURE_COOKIE]) as browser:
        assert (await browser.get("/auth/me")).status_code == 200


async def test_a_logout_refuses_the_login_it_overtakes(
    app: FastAPI, gate: Gate, operator_sessions: OperatorSessionStore
) -> None:
    pending = await _session(app, "/test-pending")

    async with _browser(app, pending) as browser, asyncio.timeout(10):
        callback = asyncio.create_task(browser.post("/test-callback"))
        await gate.reached()
        assert (await browser.post("/auth/logout", headers={"Origin": APP_URL})).status_code == 303
        gate.open()
        assert (await callback).status_code == 401
    assert await _rows(operator_sessions) == []


async def test_shape_reads_revalidate_where_other_reads_are_not_stored(app: FastAPI, folded: UUID) -> None:
    """The Electric proxy's `private, no-cache` lets a browser re-read a shape by its entity tag;
    `no-store` would have it fetch the whole shape again on every read."""
    cookie = await _session(app, "/test-login")
    async with _browser(app, cookie) as browser:
        scope = await browser.get(f"/threads/{folded}/sync/scope")
        shape = {"projection_epoch": scope.json()["projection_epoch"], "offset": "-1"}
        first = await browser.get(f"/threads/{folded}/sync/entities", params=shape)
        again = await browser.get(
            f"/threads/{folded}/sync/entities", params=shape, headers={"if-none-match": first.headers["etag"]}
        )

    assert (first.status_code, again.status_code) == (200, 304)
    assert [first.headers["cache-control"], again.headers["cache-control"]] == ["private, no-cache"] * 2
    assert scope.headers["cache-control"] == "no-store"


if __name__ == "__main__":
    pytest_bazel.main()
