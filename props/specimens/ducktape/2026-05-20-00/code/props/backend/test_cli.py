"""Tests for _GraderSpawningServer startup path."""

from __future__ import annotations

import asyncio
import contextlib
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
    """When grader_supervisor is on app.state, startup creates a spawn task."""
    config = uvicorn.Config(app_with_grader, host="127.0.0.1", port=0)
    server = _GraderSpawningServer(config, app=app_with_grader)

    with patch.object(uvicorn.Server, "startup", new_callable=AsyncMock):
        await server.startup()

    # spawn_existing should have been scheduled as a task
    app_with_grader.state.grader_supervisor.spawn_existing.assert_called_once()
    # Clean up the background task
    if hasattr(server, "_spawn_task"):
        server._spawn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server._spawn_task


async def test_startup_skips_when_no_grader(app_without_grader: FastAPI) -> None:
    """When grader_supervisor is None, startup completes without error."""
    config = uvicorn.Config(app_without_grader, host="127.0.0.1", port=0)
    server = _GraderSpawningServer(config, app=app_without_grader)

    with patch.object(uvicorn.Server, "startup", new_callable=AsyncMock):
        await server.startup()

    assert not hasattr(server, "_spawn_task")


async def test_startup_skips_when_no_state_attr() -> None:
    """When app.state has no grader_supervisor attribute at all, startup is fine."""
    app = FastAPI()
    # Don't set grader_supervisor at all — getattr returns None
    config = uvicorn.Config(app, host="127.0.0.1", port=0)
    server = _GraderSpawningServer(config, app=app)

    with patch.object(uvicorn.Server, "startup", new_callable=AsyncMock):
        await server.startup()

    assert not hasattr(server, "_spawn_task")


if __name__ == "__main__":
    pytest_bazel.main()
