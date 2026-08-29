"""Shared fixtures for the egress-proxy integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from haku.egress.testing.proxy_test_harness import RecordingUpstream, recording_upstream


@pytest.fixture
async def upstream() -> AsyncIterator[RecordingUpstream]:
    async with recording_upstream("127.0.0.1") as recording:
        yield recording
