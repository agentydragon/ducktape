"""Tests for _GraderSpawningServer startup path."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI

from props.backend.cli import _GraderSpawningServer


@pytest.fixture
def app_with_grader() -> FastAPI:
    """FastAPI app with a mock grader supervisor on state."""
    app = FastAPI()
    app.state.grader_supervisor = AsyncMock()
    app.state.grader_supervisor.spawn_existing = AsyncMock()
    return app


@pytest.fixture
def app_without_grader() -> FastAPI:
    """FastAPI app with grader_supervisor explicitly set to None."""
    app = FastAPI()
    app.state.grader_supervisor = None
    return app


async def test_startup_calls_spawn_existing(app_with_grader: FastAPI) -> None:
    """When grader_supervisor is on app.state, startup spawns graders for existing snapshots."""
    spawned = asyncio.Event()
    app_with_grader.state.grader_supervisor.spawn_existing.side_effect = spawned.set
    config = uvicorn.Config(app_with_grader, host="127.0.0.1", port=0)
    server = _GraderSpawningServer(config, app=app_with_grader)

    with patch.object(uvicorn.Server, "startup", new_callable=AsyncMock):
        await server.startup()

    async with asyncio.timeout(5):
        await spawned.wait()
    app_with_grader.state.grader_supervisor.spawn_existing.assert_awaited_once()


async def test_startup_skips_when_no_grader(app_without_grader: FastAPI) -> None:
    """When grader_supervisor is None, startup completes without error."""
    config = uvicorn.Config(app_without_grader, host="127.0.0.1", port=0)
    server = _GraderSpawningServer(config, app=app_without_grader)

    with patch.object(uvicorn.Server, "startup", new_callable=AsyncMock):
        await server.startup()


if __name__ == "__main__":
    pytest_bazel.main()
