"""Selectable numeric backend for the simulation core (NumPy reference vs JAX).

The migration to a JAX simulation core (see <augur/plans/jax_migration.md>) runs the JAX
implementation in parallel with the existing NumPy reference. `current_backend()` selects which one
runs; tests parametrize over both and assert the same invariants against each. The default is NumPy
(the reference); set `AUGUR_SIM_BACKEND=jax` (or call `use_backend(...)`) to exercise the JAX path.

This is a leaf module (only stdlib) so both the model layer (sampling) and the sim engine can read it
without a dependency cycle.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

_ENV_VAR = "AUGUR_SIM_BACKEND"


class SimBackend(StrEnum):
    NUMPY = "numpy"
    JAX = "jax"


# A ContextVar (not a module global) so the selection is thread-safe — the API server dispatches
# requests concurrently — and naturally scoped/reset by `use_backend`. Unset by default; the
# fallback (env var, else NumPy) is passed to `.get()` rather than baked into the ContextVar.
_ENV_DEFAULT = SimBackend(os.environ.get(_ENV_VAR, SimBackend.NUMPY))
_backend: ContextVar[SimBackend] = ContextVar("augur_sim_backend")


def current_backend() -> SimBackend:
    return _backend.get(_ENV_DEFAULT)


@contextmanager
def use_backend(backend: SimBackend) -> Iterator[None]:
    """Temporarily select a backend (tests parametrize over both via this)."""
    token = _backend.set(backend)
    try:
        yield
    finally:
        _backend.reset(token)
