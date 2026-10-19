"""A browser's long-lived stream request against the real app and its session middleware.

The stream is driven straight through the ASGI app, because httpx's own ASGI transport would buffer
a body that never ends; logins and logouts go through httpx. `with_test_login` adds the login route
that needs no identity provider.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI, Request
from starlette.types import Message, Scope

from agentplane.app.oidc import INSECURE_COOKIE, OIDCSettings
from agentplane.app.operator_sessions import OperatorSession, request_session

APP_URL = "http://test-app.invalid"
OIDC = OIDCSettings(
    issuer="https://test-idp.invalid/",
    client_id="test-app",
    client_secret="test-client-secret",  # a test literal, not a real credential
    session_secret="test-session-secret",  # a test literal, not a real credential
    public_base_url=APP_URL,
)


class Upstream(httpx.AsyncByteStream):
    """One upstream response body: whatever is put in `frames`, in order, waiting when it runs dry.
    An exception is raised where it stands, and None ends the body cleanly."""

    def __init__(self, *frames: bytes | Exception | None) -> None:
        self.frames: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (frame := await self.frames.get()) is not None:
            if isinstance(frame, Exception):
                raise frame
            yield frame

    async def aclose(self) -> None:
        self.closed = True


class EventSource:
    """The browser's GET of `path`, carrying `cookie`."""

    def __init__(self, app: FastAPI, cookie: str, path: str, query: bytes = b"") -> None:
        self.messages: list[Message] = []
        self.chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "query_string": query,
            "headers": [(b"cookie", f"{INSECURE_COOKIE}={cookie}".encode())],
        }
        self._served = asyncio.create_task(app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.chunks.put_nowait(message["body"])

    async def next_chunk(self) -> bytes:
        async with asyncio.timeout(10):
            return await self.chunks.get()

    def disconnect(self) -> None:
        self._disconnected.set()

    async def ended(self) -> list[Message]:
        async with asyncio.timeout(10):
            await self._served
        return self.messages


def with_test_login(app: FastAPI) -> FastAPI:
    @app.post("/test-login")
    async def login(request: Request, seconds: float) -> None:
        request_session(request).log_in(
            OperatorSession(issuer=OIDC.issuer, subject="test-subject", username="test-operator"),
            absolute_expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )

    return app


async def login(app: FastAPI, *, seconds: float = 3600) -> str:
    """A new operator session's cookie."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as browser:
        (await browser.post("/test-login", params={"seconds": seconds})).raise_for_status()
        return browser.cookies[INSECURE_COOKIE]


async def logout(app: FastAPI, cookie: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        response = await browser.post("/auth/logout", headers={"Origin": APP_URL})
        assert response.status_code == 303, response.text
