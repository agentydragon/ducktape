import asyncio
import json
from collections.abc import AsyncIterator
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


async def test_view_event_updates_dbus_and_raw_offline_cache(tmp_path: Path) -> None:
    cache = QuotaCache(tmp_path / "quotas.json")
    daemon = QuotaDaemon(Config(remote_api=RemoteAPISettings(url="https://example.test")), cache=cache)
    try:
        recorder = SignalRecorder()
        ServiceInterface._add_bus(daemon.bus_interface, cast(MessageBus, recorder))
        introspection = daemon.bus_interface.introspect()

        assert [(method.name, method.out_signature) for method in introspection.methods] == [("GetView", "s")]
        assert {prop.name: prop.signature for prop in introspection.properties} == {"Error": "s", "Status": "s"}
        assert [(signal.name, signal.signature) for signal in introspection.signals] == [("ViewChanged", "s")]

        payload = view_payload()
        raw_json = json.dumps(payload, separators=(",", ":"))
        assert daemon.accept_event(ServerSentEvent(event="view", data=raw_json))

        assert json.loads(daemon.view_json) == payload
        assert daemon.status == "connected"
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
    task = asyncio.create_task(daemon.stream_forever())
    try:
        await asyncio.wait_for(task, timeout=1)
        assert attempts == 2
        assert daemon.status == "disconnected"
        assert "stream ended" in daemon.error
        assert cache.read() is not None
        assert json.loads(daemon.view_json) == view_payload()
        assert any(message[2] == "ViewChanged" for message in recorder.messages)
    finally:
        if not task.done():
            daemon.stop_event.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await http.aclose()


async def test_stream_cancellation_stops_backoff(tmp_path: Path) -> None:
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
    task = asyncio.create_task(daemon.stream_forever())
    try:
        await asyncio.wait_for(request_started.wait(), timeout=1)
        assert daemon.status == "disconnected"
        assert "test-token" not in daemon.error
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await http.aclose()


def test_error_text_redacts_token_before_applying_bound() -> None:
    token = "secret-token"
    assert _bounded_error(RuntimeError(f"prefix {token} suffix"), token) == "prefix [redacted] suffix"
    assert "secret" not in _bounded_error(RuntimeError(f"{token}{'x' * 500}"), token)


if __name__ == "__main__":
    pytest_bazel.main()
