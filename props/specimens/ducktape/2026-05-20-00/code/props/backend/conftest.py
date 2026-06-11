"""Shared fixtures for props.backend tests."""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture
def exhaust_generator():
    """Exhaust a FastAPI dependency generator, returning the yielded value."""

    def _exhaust(gen):
        value = next(gen)
        with contextlib.suppress(StopIteration):
            next(gen)
        return value

    return _exhaust
