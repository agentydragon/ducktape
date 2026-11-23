"""Admission and lifecycle tests for owner-operated browser viewing."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_bazel
from starlette.datastructures import Headers
from websockets.asyncio.server import serve
from websockets.typing import Subprotocol

from platform_sessions.provider.interactive_browser import BrowserBusy, InteractiveBrowserRuntime

_TOKEN = "owner-gateway-secret-with-at-least-32-characters"


class FakeWebSocket:
    def __init__(self, token: str = _TOKEN) -> None:
        self.headers = Headers({"authorization": "Bearer " + token})
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


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.navigations: list[str] = []
        self.closes = 0

    def goto(self, url: str, **kwargs: Any) -> None:
        del kwargs
        self.navigations.append(url)

    def close(self) -> None:
        self.closes += 1


def runtime(tmp_path: Path, **kwargs: Any) -> InteractiveBrowserRuntime:
    result = InteractiveBrowserRuntime(
        profile_dir=tmp_path / "profile", initial_url="https://example.test/login", owner_token=_TOKEN, **kwargs
    )
    result._state = "running"
    result._thread = threading.current_thread()
    return result


@contextlib.asynccontextmanager
async def admitted_viewer(
    browser: InteractiveBrowserRuntime,
) -> AsyncIterator[tuple[FakeWebSocket, asyncio.Task[None]]]:
    """Admit a viewer through `browser.websocket` against a local echo server; yield it and its serving task."""

    async def echo(local) -> None:
        async for message in local:
            await local.send(message)

    async with serve(echo, "127.0.0.1", 0, subprotocols=[Subprotocol("binary")]) as server:
        browser._websockify_port = server.sockets[0].getsockname()[1]
        viewer = FakeWebSocket()
        active = asyncio.create_task(browser.websocket(viewer))  # type: ignore[arg-type]
        await asyncio.wait_for(viewer.accepted.wait(), timeout=2)
        yield viewer, active


def test_blank_start_does_not_navigate(tmp_path: Path) -> None:
    browser = InteractiveBrowserRuntime(profile_dir=tmp_path / "profile", owner_token=_TOKEN)
    page = FakePage("about:blank")
    context = SimpleNamespace(pages=[page])
    browser._prepare_initial_page(context)
    assert page.navigations == []
    assert context.pages == [page]

    with pytest.raises(ValueError, match="fixed HTTPS URL"):
        InteractiveBrowserRuntime(profile_dir=tmp_path / "profile", owner_token=_TOKEN, initial_url="about:blank")


def test_blank_start_quiesces_restored_pages_before_reporting_ready(tmp_path: Path) -> None:
    browser = InteractiveBrowserRuntime(profile_dir=tmp_path / "profile", owner_token=_TOKEN)
    first = FakePage("https://example.test/login")
    extra = FakePage("https://example.test/login")
    browser._prepare_initial_page(SimpleNamespace(pages=[first, extra]))
    assert (first.closes, extra.closes) == (0, 1)
    assert first.navigations == ["about:blank"]
    assert extra.navigations == []


def test_explicit_initial_url_navigates_blank_startup_page(tmp_path: Path) -> None:
    browser = InteractiveBrowserRuntime(
        profile_dir=tmp_path / "profile", owner_token=_TOKEN, initial_url="https://example.test/login"
    )
    page = FakePage("about:blank")
    browser._prepare_initial_page(SimpleNamespace(pages=[page]))
    assert page.navigations == ["https://example.test/login"]


async def test_viewer_requires_gateway_bearer_and_marks_session_before_accept(tmp_path: Path) -> None:
    events: list[str] = []

    async def on_connect() -> None:
        events.append("unverified")

    async def on_disconnect() -> None:
        events.append("disconnected")

    browser = runtime(tmp_path, on_viewer_connect=on_connect, on_viewer_disconnect=on_disconnect)
    denied = FakeWebSocket("wrong-secret-with-at-least-32-characters")
    await browser.websocket(denied)  # type: ignore[arg-type]
    assert denied.close_code == 4401
    assert events == []

    async with admitted_viewer(browser) as (viewer, active):
        assert events == ["unverified"]
        assert viewer.subprotocol == "binary"
        assert browser.viewer_connected
        with pytest.raises(BrowserBusy):
            await asyncio.to_thread(browser.run_sync, lambda _: None)
        viewer.disconnected.set()
        await asyncio.wait_for(active, timeout=2)
    assert events == ["unverified", "disconnected"]
    assert not browser.viewer_connected


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
        await browser.websocket(viewer)  # type: ignore[arg-type]
        assert viewer.close_code == 1013
        assert not viewer.accepted.is_set()
        assert not browser.viewer_connected
    finally:
        release.set()
        browser._thread.join(timeout=2)
    assert await operation == "done"


async def test_viewer_task_cancellation_finishes_unverified_transition(tmp_path: Path) -> None:
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()
    transitions: list[str] = []

    async def on_disconnect() -> None:
        transition_started.set()
        await release_transition.wait()
        transitions.append("unverified")

    browser = runtime(tmp_path, on_viewer_disconnect=on_disconnect)

    async with admitted_viewer(browser) as (_, active):
        active.cancel()
        await asyncio.wait_for(transition_started.wait(), timeout=2)
        assert not active.done()
        release_transition.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, timeout=2)
    assert transitions == ["unverified"]
    assert not browser.viewer_connected


async def test_disconnect_callback_keeps_data_plane_fenced_until_quiescent(tmp_path: Path) -> None:
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()

    async def on_disconnect() -> None:
        transition_started.set()
        await release_transition.wait()

    browser = runtime(tmp_path, on_viewer_disconnect=on_disconnect)

    async with admitted_viewer(browser) as (viewer, active):
        viewer.disconnected.set()
        await asyncio.wait_for(transition_started.wait(), timeout=2)
        assert browser.viewer_connected
        with pytest.raises(BrowserBusy):
            await asyncio.to_thread(browser.run_sync, lambda _: None)
        release_transition.set()
        await asyncio.wait_for(active, timeout=2)
    assert not browser.viewer_connected


async def test_failed_disconnect_callback_stops_browser_before_reopening_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def on_disconnect() -> None:
        raise RuntimeError("sensitive upstream error")

    browser = runtime(tmp_path, on_viewer_disconnect=on_disconnect)
    stopped = threading.Event()

    def stop_blocking() -> None:
        stopped.set()

    monkeypatch.setattr(browser, "_stop_blocking", stop_blocking)

    async with admitted_viewer(browser) as (viewer, active):
        viewer.disconnected.set()
        await asyncio.wait_for(active, timeout=2)
    assert stopped.is_set()
    assert browser._state == "stopped"
    with pytest.raises(RuntimeError, match="not running"):
        browser.run_sync(lambda _: None)
    retry = FakeWebSocket()
    await browser.websocket(retry)  # type: ignore[arg-type]
    assert retry.close_code == 1013


def test_stalled_worker_is_retained_after_cleanup_failure(tmp_path: Path) -> None:
    browser = runtime(tmp_path)

    class StalledThread(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return True

    stalled = StalledThread()
    browser._thread = stalled
    with pytest.raises(RuntimeError, match="worker did not stop"):
        browser._stop_blocking()
    assert browser._thread is stalled


async def test_cancelled_start_waits_for_worker_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = InteractiveBrowserRuntime(
        profile_dir=tmp_path / "profile", initial_url="https://example.test/login", owner_token=_TOKEN
    )
    entered = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def start_blocking() -> None:
        entered.set()
        release.wait(timeout=2)
        events.append("startup finished")

    def stop_blocking() -> None:
        events.append("cleanup")

    monkeypatch.setattr(browser, "_start_blocking", start_blocking)
    monkeypatch.setattr(browser, "_stop_blocking", stop_blocking)
    starting = asyncio.create_task(browser.start())
    assert await asyncio.to_thread(entered.wait, 2)
    starting.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert events == ["startup finished", "cleanup"]


async def test_worker_exit_during_startup_cannot_make_runtime_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = InteractiveBrowserRuntime(
        profile_dir=tmp_path / "profile", initial_url="https://example.test/login", owner_token=_TOKEN
    )
    events: list[str] = []

    def start_blocking() -> None:
        browser._thread = threading.Thread(target=lambda: None)
        browser._thread.start()
        browser._thread.join()

    def stop_blocking() -> None:
        events.append("cleanup")

    monkeypatch.setattr(browser, "_start_blocking", start_blocking)
    monkeypatch.setattr(browser, "_stop_blocking", stop_blocking)
    with pytest.raises(RuntimeError, match="worker stopped during startup"):
        await browser.start()
    assert events == ["cleanup"]
    with pytest.raises(RuntimeError, match="not running"):
        browser.run_sync(lambda _: None)


def test_dead_browser_worker_cannot_admit_new_operation(tmp_path: Path) -> None:
    browser = runtime(tmp_path)
    browser._thread = threading.Thread(target=lambda: None)
    browser._thread.start()
    browser._thread.join()
    with pytest.raises(RuntimeError, match="worker has stopped"):
        browser.run_sync(lambda _: None)
    assert browser._jobs.empty()


def test_viewer_page_operation_requires_authenticated_admission(tmp_path: Path) -> None:
    browser = runtime(tmp_path)
    operations: list[str] = []
    with pytest.raises(RuntimeError, match="not admitted"):
        browser.run_viewer_sync(lambda _: operations.append("navigated"))
    assert operations == []


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"service_workers": "allow"},
        {"service_workers": "block", "proxy": {"server": "http://proxy.example.test:8080"}},
        {"service_workers": "block", "args": ["--remote-debugging-address=0.0.0.0"]},
        {"service_workers": "block", "record_har_content": "embed"},
    ],
)
def test_cdp_configuration_refuses_unsupported_or_partial_options(tmp_path: Path, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"CDP launch|record_har_content"):
        InteractiveBrowserRuntime(
            profile_dir=tmp_path / "profile", owner_token=_TOKEN, launch_mode="cdp", launch_options=options
        )


if __name__ == "__main__":
    pytest_bazel.main()
