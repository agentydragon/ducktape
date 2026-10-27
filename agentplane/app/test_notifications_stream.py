"""The notification streams behind the real session middleware, over a scripted Notification Service.

The browser's side is driven straight through the ASGI app, because httpx's own ASGI transport would
buffer a body that never ends; logins and logouts go through httpx.
"""

import asyncio
import json
from typing import Any
from uuid import UUID

import grpc
import pytest
import pytest_bazel
from fastapi import FastAPI
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import app_services, create_app
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.app.testing.operator_streams import OIDC, EventSource, login, logout, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.notification_service import notifications_operator_pb2 as pb
from agentplane.postgres.wakeups import WakeupListener
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace

SANDBOX = "test-notified-sandbox"
THREADS = [UUID("00000000-0000-4000-8000-0000000000c1"), UUID("00000000-0000-4000-8000-0000000000c2")]
OBSERVED = Timestamp()
OBSERVED.FromJsonString("2026-09-21T12:00:00Z")


@pytest.fixture
def app(
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
) -> FastAPI:
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
            notifications=notifications,
            system_status=answering_system_status(),
        )
    )


def snapshot(frame: bytes) -> dict[str, Any]:
    event, data = frame.decode().removesuffix("\n\n").split("\n")
    assert event == "event: snapshot"
    parsed: dict[str, Any] = json.loads(data.removeprefix("data: "))
    return parsed


async def test_a_sandbox_stream_follows_the_current_incarnation(
    app: FastAPI,
    notifications_operator: FakeNotificationsOperator,
    namespace: SandboxNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw, _ = await namespace.runner(SANDBOX)
    notifications_operator.sandbox_script.put_nowait(
        pb.WatchSandboxNotificationsResponse(snapshot=pb.SandboxNotificationStatus(observed_at=OBSERVED))
    )
    notifications_operator.sandbox_script.put_nowait(pb.WatchSandboxNotificationsResponse(reconnect_required=Empty()))
    cookie = await login(app)

    browser = EventSource(app, cookie, f"/sandboxes/{SANDBOX}/notifications/stream")

    assert snapshot(await browser.next_chunk()) == {"observedAt": "2026-09-21T12:00:00Z", "inboxes": []}
    # Planned renewal ends the response cleanly, and the browser opens the stream again.
    messages = await browser.ended()
    assert messages[0]["status"] == 200
    assert "interrupted" not in caplog.text
    assert notifications_operator.sandbox_requests == [
        pb.WatchSandboxNotificationsRequest(namespace=namespace.name, name=SANDBOX, uid=raw["metadata"]["uid"])
    ]


async def test_a_thread_stream_follows_the_threads_sessions(
    app: FastAPI, notifications_operator: FakeNotificationsOperator
) -> None:
    notifications_operator.session_script.put_nowait(
        pb.WatchSessionNoticesResponse(
            snapshot=pb.SessionNoticeStates(
                observed_at=OBSERVED,
                inboxes=[pb.InboxNoticeState(inbox_id="inbox-a", session_id=str(THREADS[1]), unacknowledged=3)],
            )
        )
    )
    notifications_operator.session_script.put_nowait(pb.WatchSessionNoticesResponse(reconnect_required=Empty()))
    cookie = await login(app)
    query = "&".join(f"thread_id={thread}" for thread in THREADS).encode()

    browser = EventSource(app, cookie, "/threads/notices/stream", query)

    (state,) = snapshot(await browser.next_chunk())["inboxes"]
    assert state["unacknowledged"] == 3
    await browser.ended()
    assert [list(request.session_ids) for request in notifications_operator.session_requests] == [
        [str(thread) for thread in THREADS]
    ]


@pytest.mark.parametrize("ending", ["failure", "disconnect", "drain", "logout"])
async def test_a_stream_ends_and_hangs_up_on_the_service(
    app: FastAPI, notifications_operator: FakeNotificationsOperator, ending: str, caplog: pytest.LogCaptureFixture
) -> None:
    notifications_operator.session_script.put_nowait(
        pb.WatchSessionNoticesResponse(snapshot=pb.SessionNoticeStates(observed_at=OBSERVED))
    )
    if ending == "failure":
        notifications_operator.session_script.put_nowait(grpc.StatusCode.UNAVAILABLE)
    cookie = await login(app)
    browser = EventSource(app, cookie, "/threads/notices/stream", f"thread_id={THREADS[0]}".encode())
    snapshot(await browser.next_chunk())

    if ending == "disconnect":
        browser.disconnect()
    elif ending == "drain":
        app_services(app).drain.begin()
    elif ending == "logout":
        await logout(app, cookie)
    messages = await browser.ended()

    assert messages[0]["status"] == 200
    assert ("Notification stream interrupted" in caplog.text) == (ending == "failure")
    if ending != "failure":
        async with asyncio.timeout(10):
            await notifications_operator.hung_up.wait()


if __name__ == "__main__":
    pytest_bazel.main()
