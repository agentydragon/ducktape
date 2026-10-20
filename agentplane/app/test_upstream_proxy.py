"""`/upstream/dispatcher` behind the real session middleware, over a scripted Dispatcher facade."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import INSECURE_COOKIE
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.operator_streams import APP_URL, OIDC, EventSource, Upstream, login, logout, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.app.upstream_proxy import UpstreamProxy
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.postgres.wakeups import WakeupListener

WATCH = "/upstream/dispatcher/v1/dispatches:watch"
FIRST = b'{"result":{"dispatch":{"commandId":"first"}}}\n'
SECOND = b'{"result":{"caughtUp":{}}}\n'


class Facade:
    """Answers each request with the next of `upstreams`, recording the requests."""

    def __init__(
        self, *upstreams: Upstream, status_code: int = 200, content_type: str = "application/x-ndjson"
    ) -> None:
        self.upstreams = upstreams
        self.status_code = status_code
        self.content_type = content_type
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                self.status_code,
                headers={"Content-Type": self.content_type},
                stream=self.upstreams[len(self.requests) - 1],
            )

        return httpx.MockTransport(answer)


def rotate(token_file: Path) -> None:
    token_file.write_text("test-dispatcher-token-2\n")  # a test literal, not a real credential


ServeApp = Callable[[httpx.AsyncBaseTransport], FastAPI]


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "dispatcher-token"
    path.write_text("test-dispatcher-token-1\n")  # a test literal, not a real credential
    return path


@pytest.fixture
async def serve(
    inventory: SandboxAccess,
    bridge: RunnerBridge,
    store: ThreadStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications: NotificationsOperatorClient,
    operator_sessions: OperatorSessionStore,
    database_updates: WakeupListener,
    token_file: Path,
) -> AsyncIterator[ServeApp]:
    clients: list[httpx.AsyncClient] = []

    def serving(transport: httpx.AsyncBaseTransport) -> FastAPI:
        facade = httpx.AsyncClient(base_url="https://test-dispatcher.invalid", transport=transport)
        clients.append(facade)
        return with_test_login(
            create_app(
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
                dispatcher_http=UpstreamProxy(facade, token_file),
                notifications=notifications,
                system_status=answering_system_status(),
            )
        )

    yield serving
    for client in clients:
        await client.aclose()


async def test_query_and_credential_reach_the_facade_and_its_answer_comes_back(
    serve: ServeApp, token_file: Path
) -> None:
    refusal = b'{"code":5,"message":"no such dispatch"}'
    facade = Facade(Upstream(refusal, None), Upstream(refusal, None), status_code=404, content_type="application/json")
    app = serve(facade.transport())
    cookie = await login(app)

    browser = EventSource(
        app, cookie, "/upstream/dispatcher/v1/dispatches", b"filter.sessionId=s&filter.commandIds=a&filter.commandIds=b"
    )
    assert await browser.next_chunk() == refusal
    messages = await browser.ended()
    # The token rotates under the app, and the next request carries the new one.
    rotate(token_file)
    await EventSource(app, cookie, "/upstream/dispatcher/v1/dispatches").ended()

    assert messages[0]["status"] == 404
    assert (b"content-type", b"application/json") in messages[0]["headers"]
    first = facade.requests[0]
    assert first.url.path == "/v1/dispatches"
    assert first.url.params.get_list("filter.commandIds") == ["a", "b"]
    assert [request.headers["Authorization"] for request in facade.requests] == [
        "Bearer test-dispatcher-token-1",
        "Bearer test-dispatcher-token-2",
    ]


async def test_a_watch_arrives_as_it_is_sent_and_ends_with_the_operator_session(serve: ServeApp) -> None:
    upstream = Upstream(FIRST)
    app = serve(Facade(upstream).transport())
    cookie = await login(app)

    browser = EventSource(app, cookie, WATCH)
    assert await browser.next_chunk() == FIRST
    upstream.frames.put_nowait(SECOND)
    assert await browser.next_chunk() == SECOND
    await logout(app, cookie)
    await browser.ended()

    assert upstream.closed


async def test_a_disconnect_closes_the_watch(serve: ServeApp) -> None:
    upstream = Upstream(FIRST)
    app = serve(Facade(upstream).transport())

    browser = EventSource(app, await login(app), WATCH)
    assert await browser.next_chunk() == FIRST
    browser.disconnect()
    await browser.ended()

    assert upstream.closed


async def test_an_unreachable_facade_is_a_503_before_the_response_starts(serve: ServeApp) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test connection failed", request=request)

    app = serve(httpx.MockTransport(fail))
    messages = await EventSource(app, await login(app), WATCH).ended()
    assert messages[0]["status"] == 503


async def test_a_command_is_not_reachable_through_the_proxy(serve: ServeApp) -> None:
    facade = Facade()
    app = serve(facade.transport())
    cookie = await login(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, cookies={INSECURE_COOKIE: cookie}
    ) as browser:
        response = await browser.post(
            "/upstream/dispatcher/v1/sessions/s:submitInput", headers={"Origin": APP_URL}, json={}
        )

    assert response.status_code == 405
    assert not facade.requests


async def test_a_caller_without_an_operator_session_gets_nothing(serve: ServeApp) -> None:
    facade = Facade()
    app = serve(facade.transport())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL) as browser:
        response = await browser.get(WATCH)

    assert response.status_code == 401
    assert not facade.requests


if __name__ == "__main__":
    pytest_bazel.main()
