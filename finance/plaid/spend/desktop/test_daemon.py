"""Tests for the desktop daemon's event-stream projection."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import cast
from unittest.mock import Mock, call

import httpx
import pytest
import pytest_bazel

from finance.plaid.spend.desktop import daemon as daemon_module
from finance.plaid.spend.desktop.daemon import PlaidSpendBusInterface, PlaidSpendDaemon
from finance.plaid.spend.desktop.oauth import AuthenticationRequiredError


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
    assert [json.loads(call.args[0]) for call in bus_interface.view_changed.call_args_list] == [first, second]
    assert daemon.view == second
    assert daemon._retry_delay == 2.0


async def test_token_refresh_reconnect_skips_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    attempts = 0
    waits: list[float] = []

    async def consume_events() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            daemon.shutting_down = True
        return attempts == 1

    async def wait_for_retry(delay: float) -> bool:
        waits.append(delay)
        raise AssertionError("token refresh should reconnect without retry backoff")

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    monkeypatch.setattr(daemon, "_wait_for_retry_or_network", wait_for_retry)
    try:
        await daemon._stream_forever()
    finally:
        await daemon.close()

    assert attempts == 2
    assert waits == []


async def test_retry_properties_track_attempt_schedule_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    bus_interface = Mock()
    daemon.bus_interface = cast(PlaidSpendBusInterface, bus_interface)
    monkeypatch.setattr(daemon_module.time, "time", lambda: 100.0)

    async def consume_events() -> bool:
        return False

    async def wait_for_retry(delay: float) -> bool:
        assert delay == 2.0
        assert daemon.next_retry_at == 102.0
        daemon.shutting_down = True
        return False

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    monkeypatch.setattr(daemon, "_wait_for_retry_or_network", wait_for_retry)
    try:
        await daemon._stream_forever()
        assert daemon.last_attempt_at == 100.0
        assert daemon.next_retry_at == 0.0
        assert call(next_retry_at=102.0) in bus_interface.update_properties.call_args_list
        assert call(next_retry_at=0.0) in bus_interface.update_properties.call_args_list
    finally:
        await daemon.close()

    assert daemon.next_retry_at == 0.0


async def test_authentication_required_does_not_schedule_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    attempts = 0

    async def consume_events() -> bool:
        nonlocal attempts
        attempts += 1
        raise AuthenticationRequiredError("Sign in again")

    async def wait_for_retry(_delay: float) -> bool:
        raise AssertionError("authentication-required must not retry")

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    monkeypatch.setattr(daemon, "_wait_for_retry_or_network", wait_for_retry)
    try:
        await daemon._stream_forever()
        assert attempts == 1
        assert daemon.status == "authentication-required"
        assert daemon.next_retry_at == 0.0
    finally:
        await daemon.close()


async def test_network_recovery_cancels_stuck_stream_before_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    first_attempt_started = asyncio.Event()
    attempts = 0
    active = 0
    maximum_active = 0

    async def consume_events() -> bool:
        nonlocal attempts, active, maximum_active
        attempts += 1
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if attempts == 1:
                first_attempt_started.set()
                await asyncio.Future()
            daemon.shutting_down = True
            return False
        finally:
            active -= 1

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    try:
        task = asyncio.create_task(daemon._stream_forever())
        async with asyncio.timeout(3):
            await first_attempt_started.wait()
        daemon.network_online()
        async with asyncio.timeout(3):
            await task
    finally:
        await daemon.close()

    assert attempts == 2
    assert maximum_active == 1
    assert daemon.next_retry_at == 0.0


async def test_network_recovery_interrupts_long_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    daemon._retry_delay = 60.0
    retry_scheduled = asyncio.Event()
    attempts = 0
    original_set_next_retry_at = daemon._set_next_retry_at

    def set_next_retry_at(timestamp: float) -> None:
        original_set_next_retry_at(timestamp)
        if timestamp > 0:
            retry_scheduled.set()

    async def consume_events() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            daemon.shutting_down = True
            return False
        raise RuntimeError("offline")

    monkeypatch.setattr(daemon, "_set_next_retry_at", set_next_retry_at)
    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    task = asyncio.create_task(daemon._stream_forever())
    try:
        async with asyncio.timeout(3):
            await retry_scheduled.wait()
        assert daemon.next_retry_at >= time.time() + 59
        daemon.network_online()
        async with asyncio.timeout(3):
            await task
    finally:
        await daemon.close()

    assert attempts == 2
    assert daemon.next_retry_at == 0.0


async def test_network_recovery_during_login_does_not_restart_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = PlaidSpendDaemon()
    first_attempt_started = asyncio.Event()
    login_started = asyncio.Event()
    finish_login = asyncio.Event()
    second_attempt_started = asyncio.Event()
    attempts = 0
    active = 0
    maximum_active = 0

    async def consume_events() -> bool:
        nonlocal attempts, active, maximum_active
        attempts += 1
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if attempts == 1:
                first_attempt_started.set()
                await asyncio.Future()
            if attempts == 2:
                second_attempt_started.set()
                daemon.shutting_down = True
            return False
        finally:
            active -= 1

    async def login() -> None:
        login_started.set()
        await finish_login.wait()

    monkeypatch.setattr(daemon, "_consume_events", consume_events)
    monkeypatch.setattr(daemon.auth, "login", login)
    daemon._set_status("connecting", "")
    daemon._ensure_stream_task()
    try:
        async with asyncio.timeout(3):
            await first_attempt_started.wait()
        daemon.start_login()
        async with asyncio.timeout(3):
            await login_started.wait()
        assert active == 0
        daemon.network_online()
        assert attempts == 1
        finish_login.set()
        assert daemon.login_task is not None
        async with asyncio.timeout(3):
            await daemon.login_task
            await second_attempt_started.wait()
        assert daemon.stream_task is not None
        async with asyncio.timeout(3):
            await daemon.stream_task
    finally:
        await daemon.close()

    assert attempts == 2
    assert maximum_active == 1


if __name__ == "__main__":
    pytest_bazel.main()
