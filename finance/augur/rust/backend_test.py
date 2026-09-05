"""The Rust engine against the shared acceptance suites.

Nothing here but the name of the engine. What it has to satisfy is not particular to it and
lives in `sim/testing/`: `engine_acceptance.py` for the contract every engine answers in, and
`tax_statute.py` for what the tax code says the answers are.

This is the substitutability claim in its strongest form. The differential suites next door
compare Rust against JAX, which is blind to a rule both implement the same way and both get
wrong; these assertions state what the answer must be, so Rust can fail them alone — or,
more usefully, fail them alongside JAX and say the rule is wrong rather than one engine.
"""

import pytest
import pytest_bazel

from finance.augur.rust.backend import RustEngine
from finance.augur.rust.result import run_rust
from finance.augur.sim.backend import Engine
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance
from finance.augur.sim.testing.rollout_independence import RolloutIndependenceAcceptance
from finance.augur.sim.testing.simulation_result import Backend
from finance.augur.sim.testing.tax_statute import TaxStatuteAcceptance


class TestRustEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return RustEngine()


class TestRustTaxStatute(TaxStatuteAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return RustEngine()


class TestRustRolloutIndependence(RolloutIndependenceAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


if __name__ == "__main__":
    pytest_bazel.main()
