"""The JAX engine against the shared acceptance suite.

Nothing here but the name of the engine: what it has to satisfy is the contract every engine
satisfies, and that lives in `sim/testing/engine_acceptance.py`.
"""

import pytest
import pytest_bazel

from finance.augur.sim.backend import Engine
from finance.augur.sim.engine.jax_backend import JaxEngine
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance


class TestJaxEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


if __name__ == "__main__":
    pytest_bazel.main()
