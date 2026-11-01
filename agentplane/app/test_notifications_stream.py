"""Notification watches through the real Go server and signed operator login."""

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty

from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.app.testing.replication_process import AppProcess
from agentplane.grpc.serving import serving
from agentplane.notification_service import notifications_operator_pb2, notifications_operator_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import SandboxCluster

# gazelle:include_dep @pypi//protobuf

NOTICES_WATCH = "/upstream/notifications/v1/operator/notices:watch"


@dataclass
class Peer:
    service: FakeNotificationsOperator
    target: str
    token: Path


@pytest.fixture
async def peer(tmp_path: Path) -> AsyncIterator[Peer]:
    service = FakeNotificationsOperator()
    server = grpc.aio.server()
    notifications_operator_pb2_grpc.add_NotificationsOperatorServicer_to_server(service, server)
    token = tmp_path / "notifications-token"
    token.write_text("test-notifications-token")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Peer(service, f"http://127.0.0.1:{port}", token)


@pytest.fixture
def app_config(peer: Peer) -> dict[str, object]:
    return {"notifications_url": peer.target, "notifications_token_file": str(peer.token)}


async def test_sandbox_watch_is_pinned_to_the_current_uid_and_renews_its_token(
    http: httpx.AsyncClient, sandbox: SandboxCluster, peer: Peer
) -> None:
    for token in ("test-notifications-token", "rotated-notifications-token"):
        peer.token.write_text(token)
        peer.service.sandbox_script.put_nowait(
            notifications_operator_pb2.WatchSandboxNotificationsResponse(
                snapshot=notifications_operator_pb2.SandboxNotificationStatus()
            )
        )
        peer.service.sandbox_script.put_nowait(
            notifications_operator_pb2.WatchSandboxNotificationsResponse(reconnect_required=Empty())
        )
        response = await http.get(f"/sandboxes/{sandbox.name}/notifications/stream")
        assert response.status_code == 200, response.text
        assert response.text.splitlines()[0] == "event: snapshot"
        assert json.loads(response.text.splitlines()[1].removeprefix("data: ")) == {"inboxes": []}
    assert (
        peer.service.sandbox_requests
        == [
            notifications_operator_pb2.WatchSandboxNotificationsRequest(
                namespace=sandbox.namespace, name=sandbox.name, uid=sandbox.uid
            )
        ]
        * 2
    )
    assert peer.service.tokens == ["Bearer test-notifications-token", "Bearer rotated-notifications-token"]


async def test_thread_watch_filters_session_ids_and_relays_protojson(http: httpx.AsyncClient, peer: Peer) -> None:
    ids = [str(uuid4()), str(uuid4())]
    peer.service.session_script.put_nowait(
        notifications_operator_pb2.WatchSessionNoticesResponse(
            snapshot=notifications_operator_pb2.SessionNoticeStates(
                inboxes=[
                    notifications_operator_pb2.InboxNoticeState(inbox_id="one", session_id=ids[1], unacknowledged=3)
                ]
            )
        )
    )
    peer.service.session_script.put_nowait(
        notifications_operator_pb2.WatchSessionNoticesResponse(reconnect_required=Empty())
    )
    response = await http.get(NOTICES_WATCH, params=[("sessionIds", value) for value in ids])
    assert response.status_code == 200, response.text
    frames = [json.loads(line)["result"] for line in response.text.splitlines()]
    data = frames[0]["snapshot"]
    assert data["inboxes"][0]["sessionId"] == ids[1]
    assert data["inboxes"][0]["unacknowledged"] == 3
    assert "reconnectRequired" in frames[1]
    assert [list(request.session_ids) for request in peer.service.session_requests] == [ids]


@pytest.mark.parametrize("ending", ["failure", "eof", "disconnect", "sigterm", "logout"])
async def test_notification_stream_closes_and_cancels_upstream(
    app: AppProcess, http: httpx.AsyncClient, peer: Peer, ending: str
) -> None:
    peer.service.session_script.put_nowait(
        notifications_operator_pb2.WatchSessionNoticesResponse(
            snapshot=notifications_operator_pb2.SessionNoticeStates()
        )
    )
    async with http.stream("GET", NOTICES_WATCH, params={"sessionIds": str(uuid4())}) as response:
        assert response.status_code == 200
        lines = response.aiter_lines()
        assert json.loads(await anext(lines))["result"]["snapshot"].get("inboxes", []) == []
        if ending == "failure":
            peer.service.session_script.put_nowait(grpc.StatusCode.UNAVAILABLE)
        elif ending == "eof":
            peer.service.session_script.put_nowait(None)
        elif ending == "sigterm":
            app.process.send_signal(signal.SIGTERM)
        elif ending == "logout":
            assert (await http.post("/auth/logout")).status_code == 303
        if ending != "disconnect":
            async with asyncio.timeout(5):
                remaining = [json.loads(line) async for line in lines]
            if ending == "failure":
                assert len(remaining) == 1
                assert remaining[0]["error"]["code"] == grpc.StatusCode.UNAVAILABLE.value[0]
                assert remaining[0]["error"]["message"] == "scripted failure"
            elif ending in {"sigterm", "logout"}:
                assert len(remaining) == 1
                assert remaining[0]["error"]["code"] == grpc.StatusCode.CANCELLED.value[0]
                assert remaining[0]["error"]["message"] == "context canceled"
            else:
                assert remaining == []
    if ending in {"disconnect", "sigterm", "logout"}:
        async with asyncio.timeout(5):
            await peer.service.hung_up.wait()


async def test_notification_watch_requires_browser_login(app: AppProcess, agent_auth: dict[str, str]) -> None:
    async with httpx.AsyncClient(base_url=app.url, headers=agent_auth) as workload:
        response = await workload.get(NOTICES_WATCH, params={"sessionIds": str(uuid4())})
    assert response.status_code == 403


if __name__ == "__main__":
    pytest_bazel.main()
