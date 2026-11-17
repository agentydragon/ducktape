"""Admission and lifecycle tests for owner-operated browser viewing."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from websockets.asyncio.server import serve
from websockets.typing import Subprotocol

from platform_sessions.provider.interactive_browser import BrowserBusy, InteractiveBrowserRuntime

_TOKEN = "owner-gateway-secret-with-at-least-32-characters"


class FakeWebSocket:
    def __init__(self, token: str = _TOKEN) -> None:
        self.headers = {"authorization": "Bearer " + token}
        self.scope: dict[str, Any] = {"subprotocols": ["binary"]}
        self.accepted = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.close_code: int | None = None
        self.subprotocol: str | None = None

    async def accept(self, *, subprotocol: str | None = None) -> None:
        self.subprotocol = subprotocol
        self.accepted.set()

    async def receive(self) -> dict[str, str]:
        await self.disconnected.wait()
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        del data

    async def send_text(self, data: str) -> None:
        del data

    async def close(self, *, code: int) -> None:
        self.close_code = code
        self.disconnected.set()


def runtime(tmp_path: Path, **kwargs: Any) -> InteractiveBrowserRuntime:
    result = InteractiveBrowserRuntime(
        profile_dir=tmp_path / "profile", initial_url="https://example.test/login", owner_token=_TOKEN, **kwargs
    )
    result._state = "running"
    result._thread = threading.current_thread()
    return result


@pytest.mark.asyncio
async def test_viewer_requires_gateway_bearer_and_marks_session_before_accept(tmp_path: Path) -> None:
    events: list[str] = []

    async def on_connect() -> None:
        events.append("unverified")

    async def on_disconnect() -> None:
        events.append("disconnected")

    browser = runtime(tmp_path, on_viewer_connect=on_connect, on_viewer_disconnect=on_disconnect)
    denied = FakeWebSocket("wrong-secret-with-at-least-32-characters")
    await browser.websocket(denied)
    assert denied.close_code == 4401
    assert events == []

    async def echo(local) -> None:
        async for message in local:
            await local.send(message)

    async with serve(echo, "127.0.0.1", 0, subprotocols=[Subprotocol("binary")]) as server:
        browser._websockify_port = server.sockets[0].getsockname()[1]
        viewer = FakeWebSocket()
        active = asyncio.create_task(browser.websocket(viewer))
        await asyncio.wait_for(viewer.accepted.wait(), timeout=2)
        assert events == ["unverified"]
        assert viewer.subprotocol == "binary"
        assert browser.viewer_connected
        with pytest.raises(BrowserBusy):
            browser.run_sync(lambda _: None)
        viewer.disconnected.set()
        await asyncio.wait_for(active, timeout=2)
    assert events == ["unverified", "disconnected"]
    assert not browser.viewer_connected


@pytest.mark.asyncio
async def test_pending_browser_operation_prevents_viewer_admission(tmp_path: Path) -> None:
    browser = runtime(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def worker() -> None:
        job = browser._jobs.get()
        assert job is not None
        entered.set()
        release.wait(timeout=2)
        job.result.set_result(job.callback(None))

    browser._thread = threading.Thread(target=worker)
    browser._thread.start()
    operation = asyncio.create_task(asyncio.to_thread(browser.run_sync, lambda _: "done"))
    assert await asyncio.to_thread(entered.wait, 2)
    viewer = FakeWebSocket()
    try:
        await browser.websocket(viewer)
        assert viewer.close_code == 1013
        assert not viewer.accepted.is_set()
        assert not browser.viewer_connected
    finally:
        release.set()
        browser._thread.join(timeout=2)
    assert await operation == "done"


if __name__ == "__main__":
    pytest_bazel.main()
