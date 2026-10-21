import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_bazel
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface
from httpx_sse import ServerSentEvent
from pydantic import SecretStr

from aiquota.cache import QuotaCache
from aiquota.config import Config, RemoteAPISettings
from aiquota.desktop import BASE_RETRY_SECONDS, QuotaDaemon, SSEProtocolError, _bounded_error

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def view_payload() -> dict[str, object]:
    fetch = {
        "fetched_at": NOW.isoformat(),
        "result": {
            "kind": "success",
            "windows": [{"used_percent": 42, "reset_seconds": 3600, "window_seconds": 18000}],
        },
    }
    return {
        "fetched_at": NOW.isoformat(),
        "providers": [
            {
                "provider": "codex",
                "last_output": fetch,
                "last_success": fetch,
                "currently_over_plan": True,
                "extra_status": "active",
                "paid_credits_active": False,
                "burn": {
                    "in_peak": True,
                    "multiplier": 2,
                    "peak_multiplier": 2,
                    "applies_to": "codex",
                    "upcoming": [{"start": NOW.isoformat(), "end": NOW.isoformat()}],
                },
            }
        ],
    }


def sse_view(payload: dict[str, object] | None = None) -> bytes:
    serialized = json.dumps(payload or view_payload(), separators=(",", ":"))
    return f"event:view\ndata:{serialized}\n\n".encode()


class SignalRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[object, ...]] = []

    def _interface_signal_notify(self, *args: object) -> None:
        self.messages.append(args)


class OneChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.chunk

    async def aclose(self) -> None:
        return None


def install_network_monitor(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    online_callbacks: list[Callable[[], None]] = []

    class FakeNetworkMonitor:
        def __init__(self, on_online: Callable[[], None]) -> None:
            online_callbacks.append(on_online)

        async def __aenter__(self) -> FakeNetworkMonitor:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("aiquota.desktop.NetworkMonitor", FakeNetworkMonitor)
    return online_callbacks


async def test_view_event_updates_dbus_and_raw_offline_cache(tmp_path: Path) -> None:
    cache = QuotaCache(tmp_path / "quotas.json")
    daemon = QuotaDaemon(Config(remote_api=RemoteAPISettings(url="https://example.test")), cache=cache)
    try:
        recorder = SignalRecorder()
        ServiceInterface._add_bus(daemon.bus_interface, cast(MessageBus, recorder))
        introspection = daemon.bus_interface.introspect()

        assert [(method.name, method.out_signature) for method in introspection.methods] == [("GetView", "s")]
        assert {prop.name: prop.signature for prop in introspection.properties} == {
            "Error": "s",
            "LastAttemptAt": "d",
            "NextRetryAt": "d",
            "Status": "s",
        }
        assert [(signal.name, signal.signature) for signal in introspection.signals] == [("ViewChanged", "s")]

        payload = view_payload()
        raw_json = json.dumps(payload, separators=(",", ":"))
        assert daemon.accept_event(ServerSentEvent(event="view", data=raw_json))

        assert json.loads(daemon.view_json) == payload
        assert daemon.status == "connected"
        assert daemon.last_attempt_at == 0
        assert daemon.next_retry_at == 0
        cached = cache.read()
        assert cached is not None
        assert cached.providers[0].provider == "codex"
        assert daemon.retry_delay == BASE_RETRY_SECONDS
        assert any(
            len(message) >= 5 and message[2] == "ViewChanged" and message[4] == [raw_json]
            for message in recorder.messages
        )
    finally:
        await daemon.http.aclose()


async def test_malformed_view_event_is_rejected_without_cache_write(tmp_path: Path) -> None:
    cache = QuotaCache(tmp_path / "quotas.json")
    daemon = QuotaDaemon(Config(remote_api=RemoteAPISettings(url="https://example.test")), cache=cache)
    try:
        with pytest.raises(SSEProtocolError):
            daemon.accept_event(ServerSentEvent(event="view", data='{"providers": "invalid"}'))
        assert cache.read() is None
        assert daemon.view_json == ""
    finally:
        await daemon.http.aclose()


async def test_stream_reconnects_and_keeps_latest_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiquota.desktop.BASE_RETRY_SECONDS", 0.01)
    install_network_monitor(monkeypatch)
    cache = QuotaCache(tmp_path / "quotas.json")
    attempts = 0
    daemon: QuotaDaemon

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path == "/v1/events"
        assert request.headers["accept"] == "text/event-stream"
        assert request.headers["authorization"] == "Bearer test-token"
        if attempts > 1:
            daemon.stop_event.set()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=OneChunkStream(b""))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=OneChunkStream(sse_view()))

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    daemon = QuotaDaemon(
        Config(remote_api=RemoteAPISettings(url="https://example.test", bearer_token=SecretStr("test-token"))),
        cache=cache,
        http=http,
    )
    recorder = SignalRecorder()
    ServiceInterface._add_bus(daemon.bus_interface, cast(MessageBus, recorder))
    statuses: list[tuple[str, str]] = []
    set_status = daemon.set_status

    def record_status(status: str, error: str = "") -> None:
        statuses.append((status, error))
        set_status(status, error)

    monkeypatch.setattr(daemon, "set_status", record_status)
    task = asyncio.create_task(daemon.stream_forever())
    try:
        await asyncio.wait_for(task, timeout=1)
        assert attempts == 2
        assert ("disconnected", "AIQuota event stream ended") in statuses
        assert statuses[-1] == ("connecting", "")
        assert daemon.status == "connecting"
        assert daemon.error == ""
        assert cache.read() is not None
        assert json.loads(daemon.view_json) == view_payload()
        assert daemon.last_attempt_at > 0
        assert daemon.next_retry_at == 0
        assert any(message[2] == "ViewChanged" for message in recorder.messages)
    finally:
        if not task.done():
            daemon.stop_event.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await http.aclose()


async def test_stream_cancellation_stops_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_network_monitor(monkeypatch)
    cache = QuotaCache(tmp_path / "quotas.json")
    request_started = asyncio.Event()

    async def fail(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        raise httpx.ConnectError("offline test-token")

    http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    daemon = QuotaDaemon(
        Config(remote_api=RemoteAPISettings(url="https://example.test", bearer_token=SecretStr("test-token"))),
        cache=cache,
        http=http,
    )
    ServiceInterface._add_bus(daemon.bus_interface, cast(MessageBus, SignalRecorder()))
    retry_scheduled = asyncio.Event()
    set_next_retry = daemon._set_next_retry

    def record_retry(retry_at: float | None) -> None:
        set_next_retry(retry_at)
        if retry_at is not None:
            retry_scheduled.set()

    monkeypatch.setattr(daemon, "_set_next_retry", record_retry)
    task = asyncio.create_task(daemon.stream_forever())
    try:
        await asyncio.wait_for(request_started.wait(), timeout=1)
        await asyncio.wait_for(retry_scheduled.wait(), timeout=1)
        assert daemon.status == "disconnected"
        assert "test-token" not in daemon.error
        assert daemon.last_attempt_at > 0
        assert daemon.next_retry_at > time.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert daemon.next_retry_at == 0
    finally:
        if not task.done():
            task.cancel()
        await http.aclose()


async def test_network_recovery_wakes_backoff_and_cancels_stuck_stream_without_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aiquota.desktop.BASE_RETRY_SECONDS", 60)
    online_callbacks = install_network_monitor(monkeypatch)
    daemon = QuotaDaemon(
        Config(remote_api=RemoteAPISettings(url="https://example.test", bearer_token=SecretStr("test-token"))),
        cache=QuotaCache(tmp_path / "quotas.json"),
    )
    recorder = SignalRecorder()
    ServiceInterface._add_bus(daemon.bus_interface, cast(MessageBus, recorder))
    first_attempt_failed = asyncio.Event()
    retry_scheduled = asyncio.Event()
    backoff_woken = asyncio.Event()
    recovery_started = asyncio.Event()
    never = asyncio.Event()
    attempts = 0
    active_connections = 0
    max_active_connections = 0

    async def connect_once() -> None:
        nonlocal attempts, active_connections, max_active_connections
        attempts += 1
        active_connections += 1
        max_active_connections = max(max_active_connections, active_connections)
        try:
            if attempts == 1:
                first_attempt_failed.set()
                raise httpx.ConnectError("offline")
            if attempts == 2:
                backoff_woken.set()
                await never.wait()
            recovery_started.set()
            await never.wait()
        finally:
            active_connections -= 1

    monkeypatch.setattr(daemon, "connect_once", connect_once)
    set_next_retry = daemon._set_next_retry

    def record_retry(retry_at: float | None) -> None:
        set_next_retry(retry_at)
        if retry_at is not None:
            retry_scheduled.set()

    monkeypatch.setattr(daemon, "_set_next_retry", record_retry)
    task = asyncio.create_task(daemon.stream_forever())
    try:
        await asyncio.wait_for(first_attempt_failed.wait(), timeout=1)
        await asyncio.wait_for(retry_scheduled.wait(), timeout=1)
        assert daemon.status == "disconnected"
        assert daemon.next_retry_at > time.time()
        assert daemon.last_attempt_at > 0

        online_callbacks[0]()
        await asyncio.wait_for(backoff_woken.wait(), timeout=1)
        assert daemon.next_retry_at == 0
        assert active_connections == 1

        online_callbacks[0]()
        await asyncio.wait_for(recovery_started.wait(), timeout=1)
        assert attempts == 3
        assert max_active_connections == 1
        assert active_connections == 1

        # A shutdown racing a recovery signal cancels the in-flight request once
        # and cannot start a fourth connection.
        online_callbacks[0]()
        daemon.stop_event.set()
        await asyncio.wait_for(task, timeout=1)
        assert attempts == 3
        assert active_connections == 0
        assert daemon.next_retry_at == 0
        property_updates = [
            message[4][1]
            for message in recorder.messages
            if len(message) >= 5
            and message[2] == "PropertiesChanged"
            and isinstance(message[4], list)
            and len(message[4]) > 1
        ]
        assert any("LastAttemptAt" in update for update in property_updates)
        assert any("NextRetryAt" in update for update in property_updates)
    finally:
        if not task.done():
            daemon.stop_event.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await daemon.http.aclose()


def test_error_text_redacts_token_before_applying_bound() -> None:
    token = "secret-token"
    assert _bounded_error(RuntimeError(f"prefix {token} suffix"), token) == "prefix [redacted] suffix"
    assert "secret" not in _bounded_error(RuntimeError(f"{token}{'x' * 500}"), token)


if __name__ == "__main__":
    pytest_bazel.main()
