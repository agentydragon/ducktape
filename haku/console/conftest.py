"""Shared fixtures for haku/console tests: a TestClient over the console app."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from haku.console.app import create_app
from haku.console.config import Settings


@pytest.fixture
def make_client() -> Callable[..., Any]:
    """Factory: a TestClient over the console app, with optional ``Settings`` overrides
    (e.g. ``launch_routine=...``, ``haku_ui_url=...``)."""

    @contextmanager
    def _make(
        *,
        tool_call_executor: Any | None = None,
        tool_call_metadata_provider: Any | None = None,
        gmail_client: Any | None = None,
        calendar_client: Any | None = None,
        **settings_overrides: Any,
    ) -> Iterator[TestClient]:
        # haku_ui_url is required; default it so callers only override what they're testing.
        settings = Settings(**{"haku_ui_url": "https://haku-ui.test", **settings_overrides})
        app = create_app(settings)
        if tool_call_executor is not None:
            app.state.tool_call_executor = tool_call_executor
        if tool_call_metadata_provider is not None:
            app.state.tool_call_metadata_provider = tool_call_metadata_provider
        if gmail_client is not None:
            app.state.gmail_client = gmail_client
        if calendar_client is not None:
            app.state.calendar_client = calendar_client
        with TestClient(app) as c:
            yield c

    return _make


@pytest.fixture
def client(make_client: Callable[..., Any]) -> Iterator[TestClient]:
    with make_client() as c:
        yield c
