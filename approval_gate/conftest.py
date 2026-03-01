"""pytest configuration for approval_gate tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from approval_gate.storage import ActionStorage


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to auto mode with function-scoped event loops."""
    config.addinivalue_line("markers", "asyncio: mark test as async")
    config.option.asyncio_mode = "auto"
    # Ensure each test gets its own event loop. The default (None → session scope)
    # causes all tests to share one loop, leading to cross-test contamination when
    # one test's background tasks or anyio cancel scopes outlive the test.
    # config.override_ini is only available from pytest 9.1+; for 9.0.x we write
    # directly to _inicache, which getini() consults on every subsequent call.
    config._inicache["asyncio_default_fixture_loop_scope"] = "function"


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncGenerator[ActionStorage]:
    """Temporary in-memory storage for tests."""
    store = await ActionStorage.initialize(tmp_path / "test.db")
    try:
        yield store
    finally:
        await store.close()
