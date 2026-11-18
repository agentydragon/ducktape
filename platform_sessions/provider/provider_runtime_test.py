"""Lifecycle tests for durable provider import ownership."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from filelock import Timeout

from platform_sessions.provider import provider_runtime
from platform_sessions.provider.instance_ownership import InstanceOwnership
from platform_sessions.provider.provider_runtime import (
    create_instance_ownership,
    create_provider_control_service,
    provider_control_lifespan,
)


class _Service:
    def __init__(self, events: list[str]):
        self.events = events
        self.shutdown_results = iter((False, True))

    async def drain(self):
        self.events.append("drain")
        return next(self.shutdown_results)

    async def close(self):
        self.events.append("ownership close")


class _BlockedService:
    def __init__(self, events: list[str], entered: asyncio.Event, release: asyncio.Event):
        self.events = events
        self.entered = entered
        self.release = release

    async def drain(self):
        self.events.append("drain entered")
        self.entered.set()
        await self.release.wait()
        self.events.append("drain complete")
        return True

    async def close(self):
        self.events.append("ownership close")


class _ClosingService:
    def __init__(self, events: list[str], ownership: dict[str, InstanceOwnership]):
        self.events = events
        self.ownership = ownership

    async def drain(self):
        self.events.append("drain")
        return True

    async def close(self):
        self.events.append("ownership close")
        for owner in self.ownership.values():
            owner.close()


class _Control:
    def __init__(self, events: list[str]):
        self.events = events

    async def start(self):
        self.events.append("control start")

    async def stop(self):
        self.events.append("control stop")


class _FailingStopControl(_Control):
    async def stop(self):
        self.events.append("control stop failed")
        raise RuntimeError("synthetic listener shutdown failure")


@pytest.mark.asyncio
async def test_control_and_owner_remain_open_until_import_drain_completes():
    events: list[str] = []
    service = _Service(events)
    control = _Control(events)

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            events.append("owner stop")

    async with provider_control_lifespan(owner_lifespan(), lambda: service, control, lambda _: None):
        events.append("serving")

    assert events == [
        "owner start",
        "control start",
        "serving",
        "drain",
        "drain",
        "control stop",
        "owner stop",
        "ownership close",
    ]


@pytest.mark.asyncio
async def test_cancellation_does_not_release_owner_or_listener_before_drain():
    events: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    service = _BlockedService(events, entered, release)
    control = _Control(events)

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            events.append("owner stop")

    async def run_lifespan():
        async with provider_control_lifespan(owner_lifespan(), lambda: service, control, lambda _: None):
            events.append("serving")

    task = asyncio.create_task(run_lifespan())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert "owner stop" not in events
    assert "control stop" not in events

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[-4:] == ["drain complete", "control stop", "owner stop", "ownership close"]


def test_instance_ownership_uses_a_child_of_instance_state_dir(tmp_path):
    state_dir = tmp_path / "instance-state"
    state_dir.mkdir()
    ownership = create_instance_ownership([SimpleNamespace(id="member", state_dir=state_dir)])
    try:
        assert set(ownership) == {"member"}
        assert (state_dir / "imports").is_dir()
        assert state_dir.is_dir()
    finally:
        for owner in ownership.values():
            owner.close()


@pytest.mark.asyncio
async def test_ownership_acquisition_failure_prevents_provider_owner_start(tmp_path):
    state_dir = tmp_path / "instance-state"
    held_ownership = InstanceOwnership(state_dir / "imports")
    events: list[str] = []

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            events.append("owner stop")

    control = _Control(events)

    with pytest.raises(Timeout):
        async with provider_control_lifespan(
            owner_lifespan(),
            lambda: create_provider_control_service(object(), [SimpleNamespace(id="member", state_dir=state_dir)]),
            control,
            lambda _: None,
        ):
            pytest.fail("owner must not start while another process owns the ownership")

    assert events == []
    held_ownership.close()


@pytest.mark.asyncio
async def test_service_slot_failure_drains_and_releases_ownership(tmp_path, monkeypatch):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"

    monkeypatch.setattr(
        provider_runtime,
        "SessionProviderControlService",
        lambda _backend, ownership: _ClosingService(events, ownership),
    )

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        yield
        events.append("owner stop")

    with pytest.raises(RuntimeError, match="service slot unavailable"):
        async with provider_control_lifespan(
            owner_lifespan(),
            lambda: create_provider_control_service(object(), [SimpleNamespace(id="member", state_dir=state_dir)]),
            _Control(events),
            lambda _service: (_ for _ in ()).throw(RuntimeError("service slot unavailable")),
        ):
            pytest.fail("service-slot failure must happen before owner startup")

    assert events == ["drain", "ownership close"]
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


@pytest.mark.asyncio
async def test_body_failure_still_releases_after_successful_owner_exit(tmp_path):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"
    ownership = create_instance_ownership([SimpleNamespace(id="member", state_dir=state_dir)])
    service = _ClosingService(events, ownership)
    control = _Control(events)

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            events.append("owner stop")

    with pytest.raises(ValueError, match="synthetic body failure"):
        async with provider_control_lifespan(owner_lifespan(), lambda: service, control, lambda _: None):
            raise ValueError("synthetic body failure")

    assert events == ["owner start", "control start", "drain", "control stop", "owner stop", "ownership close"]
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


@pytest.mark.asyncio
async def test_listener_shutdown_failure_retains_ownership(tmp_path):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"
    ownership = create_instance_ownership([SimpleNamespace(id="member", state_dir=state_dir)])
    service = _ClosingService(events, ownership)

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            events.append("owner stop")

    with pytest.raises(RuntimeError, match="listener shutdown failure"):
        async with provider_control_lifespan(
            owner_lifespan(), lambda: service, _FailingStopControl(events), lambda _: None
        ):
            pass

    assert events == ["owner start", "control start", "drain", "control stop failed", "owner stop"]
    with pytest.raises(Timeout):
        InstanceOwnership(state_dir / "imports")
    await service.close()
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


@pytest.mark.asyncio
async def test_ownership_is_not_released_until_provider_owner_exits(tmp_path):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"
    ownership = create_instance_ownership([SimpleNamespace(id="member", state_dir=state_dir)])
    service = _ClosingService(events, ownership)
    control = _Control(events)

    @asynccontextmanager
    async def owner_lifespan():
        events.append("owner start")
        try:
            yield
        finally:
            with pytest.raises(Timeout):
                InstanceOwnership(state_dir / "imports")
            events.append("owner stop")

    async with provider_control_lifespan(owner_lifespan(), lambda: service, control, lambda _: None):
        events.append("serving")

    assert events == [
        "owner start",
        "control start",
        "serving",
        "drain",
        "control stop",
        "owner stop",
        "ownership close",
    ]
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


@pytest.mark.asyncio
async def test_failed_provider_owner_start_retains_ownership_until_process_exit(tmp_path, monkeypatch):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"
    services: list[_ClosingService] = []

    def service_constructor(_backend, ownership):
        service = _ClosingService(events, ownership)
        services.append(service)
        return service

    monkeypatch.setattr(provider_runtime, "SessionProviderControlService", service_constructor)

    class FailingOwner:
        async def __aenter__(self):
            events.append("owner start")
            raise RuntimeError("synthetic owner startup failure")

        async def __aexit__(self, *_args):
            events.append("owner stop")

    with pytest.raises(RuntimeError, match="synthetic"):
        async with provider_control_lifespan(
            FailingOwner(),
            lambda: create_provider_control_service(object(), [SimpleNamespace(id="member", state_dir=state_dir)]),
            _Control(events),
            lambda _: None,
        ):
            pytest.fail("owner startup should fail before serving")

    assert events == ["owner start", "drain"]
    with pytest.raises(Timeout):
        InstanceOwnership(state_dir / "imports")

    await services[0].close()
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


@pytest.mark.asyncio
async def test_failed_provider_owner_exit_retains_ownership_until_process_exit(tmp_path, monkeypatch):
    events: list[str] = []
    state_dir = tmp_path / "instance-state"
    services: list[_ClosingService] = []

    def service_constructor(_backend, ownership):
        service = _ClosingService(events, ownership)
        services.append(service)
        return service

    monkeypatch.setattr(provider_runtime, "SessionProviderControlService", service_constructor)

    class FailingOwner:
        async def __aenter__(self):
            events.append("owner start")

        async def __aexit__(self, *_args):
            events.append("owner stop failed")
            raise RuntimeError("synthetic owner shutdown failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        async with provider_control_lifespan(
            FailingOwner(),
            lambda: create_provider_control_service(object(), [SimpleNamespace(id="member", state_dir=state_dir)]),
            _Control(events),
            lambda _: None,
        ):
            pass

    assert events == ["owner start", "control start", "drain", "control stop", "owner stop failed"]
    with pytest.raises(Timeout):
        InstanceOwnership(state_dir / "imports")

    await services[0].close()
    successor = InstanceOwnership(state_dir / "imports")
    successor.close()


def test_service_constructor_failure_releases_instance_ownership(tmp_path, monkeypatch):
    state_dir = tmp_path / "instance-state"
    instance = SimpleNamespace(id="member", state_dir=state_dir)

    def fail_constructor(*_args, **_kwargs):
        raise ValueError("synthetic service construction failure")

    monkeypatch.setattr(provider_runtime, "SessionProviderControlService", fail_constructor)
    with pytest.raises(ValueError, match="synthetic"):
        create_provider_control_service(object(), [instance])

    successor = InstanceOwnership(state_dir / "imports")
    successor.close()
