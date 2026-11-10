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

from agentplane.app.testing.notifications import FakeNotifications
from agentplane.app.testing.replication_process import AppProcess
from agentplane.grpc.serving import serving
from agentplane.notification_service import notifications_pb2 as pb, notifications_pb2_grpc as rpc

# gazelle:include_dep @pypi//protobuf


# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)


def watch(session_id: str) -> str:
    return f"/upstream/notifications/v1/sessions/{session_id}/inbox:watch"


@dataclass
class Peer:
    service: FakeNotifications
    target: str
    token: Path


@pytest.fixture
async def peer(tmp_path: Path) -> AsyncIterator[Peer]:
    service = FakeNotifications()
    server = grpc.aio.server()
    rpc.add_NotificationsServicer_to_server(service, server)
    token = tmp_path / "notifications-token"
    token.write_text("test-notifications-token")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Peer(service, f"http://127.0.0.1:{port}", token)


@pytest.fixture
def app_config(peer: Peer) -> dict[str, object]:
    return {"notifications_url": peer.target, "notifications_token_file": str(peer.token)}


async def test_session_watch_binds_session_and_renews_its_service_token(http: httpx.AsyncClient, peer: Peer) -> None:
    session_id = str(uuid4())
    for token in ("test-notifications-token", "rotated-notifications-token"):
        peer.token.write_text(token)
        peer.service.inbox_script.put_nowait(pb.WatchInboxResponse(inbox=pb.Inbox(session_id=session_id)))
        peer.service.inbox_script.put_nowait(None)
        response = await http.get(watch(session_id))
        assert response.status_code == 200, response.text
        assert json.loads(response.text.splitlines()[0])["result"]["inbox"]["sessionId"] == session_id
    assert peer.service.requests == [pb.WatchInboxRequest(session_id=session_id)] * 2
    assert peer.service.tokens == ["Bearer test-notifications-token", "Bearer rotated-notifications-token"]


@pytest.mark.parametrize("ending", ["failure", "eof", "disconnect", "sigterm", "logout"])
async def test_notification_stream_closes_and_cancels_upstream(
    app: AppProcess, http: httpx.AsyncClient, peer: Peer, ending: str
) -> None:
    peer.service.inbox_script.put_nowait(pb.WatchInboxResponse(no_inbox=Empty()))
    async with http.stream("GET", watch(str(uuid4()))) as response:
        assert response.status_code == 200
        lines = response.aiter_lines()
        assert json.loads(await anext(lines))["result"]["noInbox"] == {}
        if ending == "failure":
            peer.service.inbox_script.put_nowait(grpc.StatusCode.UNAVAILABLE)
        elif ending == "eof":
            peer.service.inbox_script.put_nowait(None)
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
        response = await workload.get(watch(str(uuid4())))
    assert response.status_code == 403


if __name__ == "__main__":
    pytest_bazel.main()
