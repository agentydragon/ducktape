"""Real loopback WebSocket: metadata is visible before Save persists the open flow."""

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web
from mitmproxy import http, io
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.save import Save
from mitmproxy.options import Options
from mitmproxy.proxy.server_hooks import ServerConnectionHookData
from mitmproxy.tools.dump import DumpMaster

from devinfra.github_api_capture import session_ws_metadata
from devinfra.github_api_capture.session_ws_metadata import SessionWebSocketMetadata


class LoopbackUpstream:
    def __init__(self, port: int) -> None:
        self.port = port
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.observed_host: str | None = None

    def server_connect(self, data: ServerConnectionHookData) -> None:
        # Test-only routing: the real forward proxy cannot dial anything except loopback.
        data.server.address = ("127.0.0.1", self.port)

    def running(self) -> None:
        self.ready.set()

    def websocket_start(self, flow: http.HTTPFlow) -> None:
        self.observed_host = flow.request.host

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self.closed.set()


async def test_open_websocket_records_before_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = 0

    async def echo(request: web.Request) -> web.WebSocketResponse:
        nonlocal connections
        connections += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(message.data)
        return ws

    app = web.Application()
    app.router.add_get("/v1/sessions/ws/test-private-session/subscribe", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin_port = runner.addresses[0][1]
    events = tmp_path / "private" / "events.jsonl"
    raw = tmp_path / "synthetic.flows"
    options = Options(listen_host="127.0.0.1", listen_port=0, confdir=str(tmp_path / "mitmproxy"))
    master = DumpMaster(options, with_termlog=False, with_dumper=False)
    recorder = SessionWebSocketMetadata()
    lifecycle = LoopbackUpstream(origin_port)
    master.addons.add(recorder, lifecycle)
    master.options.update(save_stream_file=str(raw), record_cloud_session_ws=True, cloud_session_ws_events=str(events))
    # Exercise the real timer without making a small test wait thirty seconds.
    monkeypatch.setattr(session_ws_metadata, "HEARTBEAT_SECONDS", 0.05)
    task = asyncio.create_task(master.run())
    try:
        async with asyncio.timeout(10):
            await lifecycle.ready.wait()
            proxy = master.addons.get("proxyserver")
            assert isinstance(proxy, Proxyserver)
            port = proxy.listen_addrs()[0][1]
            url = "http://claude.ai/v1/sessions/ws/test-private-session/subscribe?token=test-private"
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect(
                    url, proxy=f"http://127.0.0.1:{port}", headers={"authorization": "test-private-token"}
                ) as ws,
            ):
                assert lifecycle.observed_host == "claude.ai"
                assert recorder.totals.flows_started == 1
                text = json.dumps({"type": "tool_progress", "tool_name": "Bash", "tool_use_id": "test-private-id"})
                await ws.send_str(text)
                assert (await ws.receive()).data == text
                await ws.send_bytes(b"test-private-binary")
                assert (await ws.receive()).data == b"test-private-binary"
                await ws.send_str("test-private-non-json")
                assert (await ws.receive()).data == "test-private-non-json"
                while True:
                    rows = [json.loads(line) for line in events.read_text().splitlines()]
                    if any(row["event"] == "heartbeat" and row["totals"]["server_messages"] == 3 for row in rows):
                        break
                    await asyncio.sleep(0.01)
                assert not ws.closed
                assert not lifecycle.closed.is_set()
                assert connections == 1
                assert recorder.totals.flows_started == 1
                assert recorder.totals.flows_ended == 0
                messages = [row for row in rows if row["event"] == "websocket_message"]
                assert len(messages) == 6
                assert len({row["flow_id"] for row in messages}) == 1
                assert {row["parse_status"] for row in messages} == {"recognized", "binary", "non_json"}
                assert "test-private" not in events.read_text()
                save = master.addons.get("save")
                assert isinstance(save, Save)
                assert save.stream is not None
                # Flush removes Python file buffering as an alternative explanation.
                save.stream.fo.flush()
                with raw.open("rb") as stream:
                    assert list(io.FlowReader(stream).stream()) == []
                assert len(save.active_flows) == 1
            await lifecycle.closed.wait()
            assert recorder.totals.flows_ended == 1
            assert any(json.loads(line)["event"] == "websocket_end" for line in events.read_text().splitlines())
    finally:
        master.shutdown()
        await asyncio.wait_for(task, timeout=5)
        await runner.cleanup()
    with raw.open("rb") as stream:
        flows = list(io.FlowReader(stream).stream())
    assert len(flows) == 1
    assert isinstance(flows[0], http.HTTPFlow)
    assert flows[0].websocket is not None
    assert len(flows[0].websocket.messages) == 6


if __name__ == "__main__":
    pytest_bazel.main()
