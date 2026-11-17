"""Process helpers for durable provider import ownership."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from os import PathLike
from pathlib import Path
from typing import Protocol

from platform_sessions.provider.import_journal import ImportJournal
from platform_sessions.provider.server import SessionProviderControlService

logger = logging.getLogger(__name__)


class _Instance(Protocol):
    id: str
    state_dir: str | PathLike[str]


class _ControlService(Protocol):
    async def drain(self) -> bool: ...

    async def close(self) -> None: ...


class _ControlListener(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def create_import_journals(instances: Iterable[_Instance]) -> dict[str, ImportJournal]:
    """Open one exclusive durable import journal below each instance state directory."""
    journals: dict[str, ImportJournal] = {}
    try:
        for instance in instances:
            journals[instance.id] = ImportJournal(Path(instance.state_dir) / "imports")
    except BaseException:
        for journal in journals.values():
            try:
                journal.close()
            except Exception:
                logger.exception("could not close provider import journal after startup failure")
        raise
    return journals


def create_provider_control_service(backend, instances: Iterable[_Instance]):
    """Create the service and journals together, releasing locks on constructor failure."""
    journals = create_import_journals(instances)
    try:
        return SessionProviderControlService(backend, journals)
    except BaseException:
        for journal in journals.values():
            try:
                journal.close()
            except Exception:
                logger.exception("could not close provider import journal after service startup failure")
        raise


async def drain_provider_control(
    service: _ControlService, control: _ControlListener, *, on_drained: Callable[[], None] | None = None
) -> None:
    """Stop admitting imports, drain them, then stop the control listener.

    The service retains journal ownership when a drain interval expires. Retry until it
    confirms that every accepted import is durable before shutting down provider owners.
    """

    async def drain_and_stop() -> None:
        try:
            while not await service.drain():
                logger.warning("Waiting for provider imports to finish before shutdown")
            if on_drained is not None:
                on_drained()
        finally:
            await control.stop()

    _, cancelled = await _finish_shielded(drain_and_stop())
    if cancelled:
        raise asyncio.CancelledError


async def drain_provider_imports(service: _ControlService) -> None:
    """Drain accepted imports without releasing journals or stopping a listener."""

    async def drain() -> None:
        while not await service.drain():
            logger.warning("Waiting for provider imports to finish before startup cleanup")

    _, cancelled = await _finish_shielded(drain())
    if cancelled:
        raise asyncio.CancelledError


async def close_provider_control(service: _ControlService) -> None:
    """Release journals only after imports and all provider owners have stopped."""

    _, cancelled = await _finish_shielded(service.close())
    if cancelled:
        raise asyncio.CancelledError


async def _finish_shielded(awaitable: Awaitable[None]) -> tuple[None, bool]:
    """Finish a cleanup task despite caller cancellation, returning its result and signal."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


class _TrackedOwnerLifespan:
    """Report successful owner exit even when the body itself raised."""

    def __init__(self, owner: AbstractAsyncContextManager[None], on_exit: Callable[[], None]):
        self.owner = owner
        self.on_exit = on_exit

    async def __aenter__(self) -> None:
        return await self.owner.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback) -> bool | None:
        suppress = await self.owner.__aexit__(exc_type, exc, traceback)
        self.on_exit()
        return suppress


@asynccontextmanager
async def provider_control_lifespan(
    provider_lifespan: AbstractAsyncContextManager[None],
    create_service: Callable[[], _ControlService],
    control: _ControlListener,
    set_service: Callable[[_ControlService], None],
) -> AsyncIterator[None]:
    """Acquire journals before starting the owner, then drain before stopping it."""
    service = create_service()
    owner_start_attempted = False
    owner_exit_status = {"exited": False}
    shutdown_status = {"imports_drained": False, "control_stopped": True}
    try:
        try:
            set_service(service)
        except BaseException:
            try:
                await drain_provider_imports(service)
            except asyncio.CancelledError:
                shutdown_status["imports_drained"] = True
                raise
            else:
                shutdown_status["imports_drained"] = True
            raise
        owner_start_attempted = True
        try:
            async with _TrackedOwnerLifespan(provider_lifespan, lambda: owner_exit_status.__setitem__("exited", True)):
                shutdown_status["control_stopped"] = False
                try:
                    await control.start()
                    yield
                finally:
                    try:
                        await drain_provider_control(
                            service, control, on_drained=lambda: shutdown_status.__setitem__("imports_drained", True)
                        )
                    except asyncio.CancelledError:
                        shutdown_status["imports_drained"] = True
                        shutdown_status["control_stopped"] = True
                        raise
                    else:
                        shutdown_status["imports_drained"] = True
                        shutdown_status["control_stopped"] = True
        except BaseException:
            if not shutdown_status["imports_drained"]:
                try:
                    await drain_provider_imports(service)
                except asyncio.CancelledError:
                    shutdown_status["imports_drained"] = True
                    raise
                else:
                    shutdown_status["imports_drained"] = True
            raise
    finally:
        # A failed owner startup or teardown may leave provider work active. Keep the
        # service referenced on app.state and retain its journal lock until process exit.
        if (
            shutdown_status["imports_drained"]
            and shutdown_status["control_stopped"]
            and (owner_exit_status["exited"] or not owner_start_attempted)
        ):
            await close_provider_control(service)
