"""Computed transitions wake SSE readers even without a database commit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import Mock

from agentplane.action_service.operator_stream import snapshot_stream
from agentplane.action_service.updates import ActionSubscription


async def test_snapshot_stream_rechecks_at_computed_expiry() -> None:
    subscription = Mock(spec=ActionSubscription)
    subscription.changed = asyncio.Event()
    expiry: datetime | None = datetime.now(UTC) + timedelta(milliseconds=50)
    snapshots = 0

    @contextmanager
    def subscribe() -> Iterator[ActionSubscription]:
        yield cast(ActionSubscription, subscription)

    async def read() -> bytes:
        nonlocal snapshots, expiry
        snapshots += 1
        if snapshots == 2:
            expiry = None
        return str(snapshots).encode()

    async def authorized() -> bool:
        return True

    response = snapshot_stream(subscribe, read, authorized, refresh_at=lambda: expiry)
    body = cast(AsyncGenerator[bytes], response.body_iterator)
    try:
        assert await anext(body) == b"event: snapshot\ndata: 1\n\n"
        async with asyncio.timeout(2):
            assert await anext(body) == b"event: snapshot\ndata: 2\n\n"
    finally:
        await body.aclose()
