"""The Rust engine against the shared acceptance suite.

Nothing here but the name of the engine: what it has to satisfy is the contract every engine
satisfies, and that lives in `sim/testing/engine_acceptance.py`.

This is the substitutability claim in its strongest form. The differential suites next door
compare Rust against JAX, which is blind to a rule both implement the same way and both get
wrong; these assertions state what the answer must be, so Rust can fail them alone.
"""

import pytest
import pytest_bazel

from finance.augur.rust.backend import RustEngine
from finance.augur.sim.backend import Engine
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance


class TestRustEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return RustEngine()


if __name__ == "__main__":
    pytest_bazel.main()
