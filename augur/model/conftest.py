"""Shared fixtures for augur.model tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from augur.model.sim_backend import SimBackend, use_backend


@pytest.fixture(params=list(SimBackend), ids=lambda backend: backend.value)
def backend(request: pytest.FixtureRequest) -> Iterator[SimBackend]:
    """Run a test against both sim backends (NumPy reference + JAX); assert the same invariants."""
    with use_backend(request.param):
        yield request.param
