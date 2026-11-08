"""Shared errors and background polling for platform session providers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from platform_sessions.probe_types import ProbeSource
from platform_sessions.registry import IntegrationInstance

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    """A sanitized outcome that an integration wants to show its owner."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


class Integration(Protocol):
    """Provider runtime operations used by background polling."""

    config: IntegrationInstance
    periodic: bool

    async def refresh(self, transport: Any = None, *, source: ProbeSource = ProbeSource.VERIFY) -> None: ...


@contextlib.asynccontextmanager
async def poll_integrations(integrations: Sequence[Integration], transport: Any = None):
    """Run each opted-in provider's existing poll operation at its interval."""

    async def poll(runtime: Integration) -> None:
        while True:
            try:
                await runtime.refresh(transport, source=ProbeSource.PERIODIC)
            except Exception:
                logger.error("Integration poll failed")  # noqa: TRY400 - exceptions may contain session data
            await asyncio.sleep(runtime.config.poll_interval_seconds)

    tasks = [asyncio.create_task(poll(runtime)) for runtime in integrations if runtime.periodic]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
