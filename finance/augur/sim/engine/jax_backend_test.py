"""The JAX engine against the shared acceptance suites.

Nothing here but the name of the engine. What it has to satisfy is not particular to it and
lives in `sim/testing/`: `engine_acceptance.py` for the contract every engine answers in, and
`tax_statute.py` for what the tax code says the answers are.
"""

import pytest
import pytest_bazel

from finance.augur.sim.backend import Engine
from finance.augur.sim.engine.jax_backend import JaxEngine
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance
from finance.augur.sim.testing.tax_statute import TaxStatuteAcceptance


class TestJaxEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


class TestJaxTaxStatute(TaxStatuteAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


if __name__ == "__main__":
    pytest_bazel.main()
