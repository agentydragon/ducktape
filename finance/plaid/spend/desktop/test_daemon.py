"""Tests for the desktop daemon's event-stream projection."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
import pytest_bazel

from finance.plaid.spend.desktop import daemon as daemon_module
from finance.plaid.spend.desktop.daemon import PlaidSpendBusInterface, PlaidSpendDaemon


class _EventStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        return None


def _event(view: Mapping[str, object]) -> str:
    return f"event: view\ndata: {json.dumps(view)}\n\n"


async def test_stream_views_are_emitted_through_the_dbus_interface() -> None:
    first = {"generated_at": "2026-10-18T12:00:00Z", "cards": []}
    second = {"generated_at": "2026-10-18T12:01:00Z", "cards": [{"label": "Updated card"}]}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (_event(first) + _event(second)).encode()
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_EventStream(body), request=request
        )

    daemon = PlaidSpendDaemon()
    await daemon.http.aclose()
    daemon.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    daemon.auth.http = daemon.http
    daemon.auth.access_token = "test-token"
    daemon.auth.access_token_expires_at = time.monotonic() + 3600
    daemon._retry_delay = 60.0
    bus_interface = Mock()
    daemon.bus_interface = cast(PlaidSpendBusInterface, bus_interface)
    try:
        assert await daemon._consume_events() is False
    finally:
        await daemon.close()

    assert [request.url.path for request in requests] == ["/api/v1/events"]
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert [json.loads(call.args[0]) for call in bus_interface.ViewChanged.call_args_list] == [first, second]
    assert daemon.view == second
    assert daemon._retry_delay == 2.0


async def test_token_refresh_reconnect_skips_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    attempts = 0
    sleeps: list[float] = []

    async def consume_events() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            daemon.shutting_down = True
        return attempts == 1

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    monkeypatch.setattr(daemon_module.asyncio, "sleep", sleep)
    try:
        await daemon._stream_forever()
    finally:
        await daemon.close()

    assert attempts == 2
    assert sleeps == []


if __name__ == "__main__":
    pytest_bazel.main()
