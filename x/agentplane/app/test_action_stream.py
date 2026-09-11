"""Exercise the SSE response boundary with a real HTTPX streaming client."""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI
from starlette.types import Message, Scope

from x.agentplane.action_service.client import CredentialPlaceholder, OperatorActionServiceClient
from x.agentplane.app.action_federation import FederatedOperatorActions
from x.agentplane.app.api import actions_router
from x.agentplane.app.identity import CallerIdentity, CallerKind, require_caller
from x.agentplane.app.oidc import OperatorSession


class StreamActions(FederatedOperatorActions):
    def __init__(self, client: OperatorActionServiceClient) -> None:
        self.client = client

    def for_session(self, session: OperatorSession) -> OperatorActionServiceClient:
        return self.client


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, fail: bool = False, wait: bool = False) -> None:
        self.fail = fail
        self.wait = wait
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: test-event\n\n"
        if self.fail:
            raise httpx.ReadError("test upstream reset")
        if self.wait:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(actions_router)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
    return app


async def request(app: FastAPI, *, disconnect: bool = False) -> list[Message]:
    messages: list[Message] = []
    first_chunk = asyncio.Event()
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/actions/stream",
        "query_string": b"",
        "headers": [],
        "session": {
            "user": OperatorSession(
                issuer="https://test-idp.invalid",
                subject="test-subject",
                username="test-operator",
                expires_at=time.time() + 3600,
            ).model_dump()
        },
    }

    async def receive() -> Message:
        if disconnect:
            await first_chunk.wait()
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async with asyncio.timeout(5):
        await app(scope, receive, send)
    return messages


@pytest.mark.parametrize("upstream_status", [401, 403, 503])
async def test_upstream_status_before_downstream_headers(app: FastAPI, upstream_status: int) -> None:
    stream = Stream()
    async with httpx.AsyncClient(
        base_url="https://test-actions.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(upstream_status, stream=stream)),
    ) as http:
        app.state.operator_actions = StreamActions(OperatorActionServiceClient(http, CredentialPlaceholder()))
        messages = await request(app)
    assert messages[0]["status"] == upstream_status
    assert stream.closed


async def test_connect_error_before_downstream_headers(app: FastAPI) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test connection failed", request=request)

    async with httpx.AsyncClient(base_url="https://test-actions.invalid", transport=httpx.MockTransport(fail)) as http:
        app.state.operator_actions = StreamActions(OperatorActionServiceClient(http, CredentialPlaceholder()))
        messages = await request(app)
    assert messages[0]["status"] == 503


@pytest.mark.parametrize("failure", ["eof", "reset", "disconnect"])
async def test_stream_cleanup(app: FastAPI, failure: str, caplog: pytest.LogCaptureFixture) -> None:
    stream = Stream(fail=failure == "reset", wait=failure == "disconnect")
    async with httpx.AsyncClient(
        base_url="https://test-actions.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    ) as http:
        app.state.operator_actions = StreamActions(OperatorActionServiceClient(http, CredentialPlaceholder()))
        messages = await request(app, disconnect=failure == "disconnect")
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b"data: test-event\n\n"
    assert stream.closed
    if failure == "reset":
        assert "Action stream interrupted" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
