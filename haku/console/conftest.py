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
    def _make(**settings_overrides: Any) -> Iterator[TestClient]:
        with TestClient(create_app(Settings(**settings_overrides))) as c:
            yield c

    return _make


@pytest.fixture
def client(make_client: Callable[..., Any]) -> Iterator[TestClient]:
    with make_client() as c:
        yield c
