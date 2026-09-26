"""Requests presenting one browser session, through `OperatorSessionMiddleware` in the app
`create_app` builds: none waits for another, and what each makes of the session survives the others.

Requests meant to overlap park at a `Gate` in their handlers -- after the middleware has read the
session, before it saves what they made of it -- or in the Action Service's answer. Each overlap is
bounded, because the regression it guards against waits for the parked request, which never comes.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from more_itertools import one
from sqlalchemy import select, update
from starlette.types import Message, Scope

from agentplane.action_service.client import OperatorActionServiceClient
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.runner.bridge import RunnerBridge
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.agent_runtime.view.content import ContentStore
from agentplane.app.api import create_app
from agentplane.app.database_updates import DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.oidc import INSECURE_COOKIE, OIDCSettings
from agentplane.app.operator_sessions import BrowserSession, OperatorSession, OperatorSessionStore, request_session
from agentplane.app.presets import Harness

APP_URL = "http://test-app.invalid"
OIDC = OIDCSettings(
    issuer="https://test-idp.invalid/",
    client_id="test-app",
    client_secret="test-client-secret",  # a test literal, not a real credential
    session_secret="test-session-secret",  # a test literal, not a real credential
    public_base_url=APP_URL,
)
LOGIN = OperatorSession(issuer=OIDC.issuer, subject="test-subject", username="test-operator")


class Gate:
    """Parks every request that reaches it until it opens."""

    def __init__(self) -> None:
        self._parked: asyncio.Queue[None] = asyncio.Queue()
        self._open = asyncio.Event()

    async def wait(self) -> None:
        self._parked.put_nowait(None)
        await self._open.wait()

    async def reached(self, count: int = 1) -> None:
        """Return once `count` more requests are parked."""
        for _ in range(count):
            await self._parked.get()

    def open(self) -> None:
        self._open.set()


class StubActions(FederatedOperatorActions):
    def __init__(self, client: OperatorActionServiceClient) -> None:
        self.client = client

    def for_request(self, request: Request) -> OperatorActionServiceClient:
        return self.client


class Tokens:
    async def token(self) -> str:
        return "test-token"


class OpenStream(httpx.AsyncByteStream):
    """An Action stream: one frame, then nothing until its reader goes."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"event: snapshot\ndata: first\n\n"
        await asyncio.Event().wait()


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
async def app(
    gate: Gate,
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: ThreadStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    event_logs: EventLogStore,
    content: ContentStore,
    operator_sessions: OperatorSessionStore,
    database_updates: DatabaseUpdates,
) -> AsyncIterator[FastAPI]:
    """The app with a login that needs no identity provider, and an Action Service that answers once
    `gate` opens."""
    app = create_app(
        inventory,
        bridge,
        store,
        {harness: ["test-model"] for harness in Harness},
        egress,
        decisions,
        live_index,
        action_policy,
        OIDC,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
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

    async def answer(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, stream=OpenStream())
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://test-actions.invalid", transport=httpx.MockTransport(answer)
    ) as http:
        app.state.operator_actions = StubActions(OperatorActionServiceClient(http, Tokens()))
        yield app
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
        pytest.param("/actions", id="action-service-answer"),
        pytest.param("/actions/stream", id="action-stream-open"),
        pytest.param("/test-held", id="slow-handler"),
    ],
)
async def test_a_request_held_before_its_headers_holds_up_no_other_on_its_session(
    app: FastAPI, gate: Gate, path: str
) -> None:
    cookie = await _session(app, "/test-login")
    held = RawRequest(app, path, cookie)
    async with asyncio.timeout(10):
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


if __name__ == "__main__":
    pytest_bazel.main()
