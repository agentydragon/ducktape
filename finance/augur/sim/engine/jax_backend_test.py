"""The JAX engine against the shared acceptance suites.

Nothing here but the name of the engine. What it has to satisfy is not particular to it and
lives in `sim/testing/`: `engine_acceptance.py` for the contract every engine answers in, and
`tax_statute.py` for what the tax code says the answers are.
"""

import pytest
import pytest_bazel

from finance.augur.sim.backend import Engine
from finance.augur.sim.engine.jax_backend import JaxEngine
from finance.augur.sim.testing.bonds import BondAcceptance, BondValueAcceptance
from finance.augur.sim.testing.cash_conservation import CashConservationAcceptance
from finance.augur.sim.testing.deductions import DeductionAcceptance
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance
from finance.augur.sim.testing.frozen_rollout import FrozenRolloutAcceptance
from finance.augur.sim.testing.income_sources import IncomeSourceAcceptance
from finance.augur.sim.testing.jax_result import run_jax
from finance.augur.sim.testing.private_equity import PrivateEquityAcceptance
from finance.augur.sim.testing.property_stakes import PropertyStakeAcceptance
from finance.augur.sim.testing.rental_lifecycle import (
    LeasingFeeAcceptance,
    ManagementFeeAcceptance,
    RentalCashflowReconciliationAcceptance,
    RentalIncomeAcceptance,
    RentalIncomeTaxationAcceptance,
    RentalLifecycleCashflowsAcceptance,
)
from finance.augur.sim.testing.rollout_independence import RolloutIndependenceAcceptance
from finance.augur.sim.testing.security_distributions import SecurityDistributionAcceptance
from finance.augur.sim.testing.simulation_result import Backend
from finance.augur.sim.testing.target_allocation import TargetAllocationAcceptance
from finance.augur.sim.testing.tax_statute import TaxStatuteAcceptance


class TestJaxEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


class TestJaxTaxStatute(TaxStatuteAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


class TestJaxRolloutIndependence(RolloutIndependenceAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxIncomeSources(IncomeSourceAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxSecurityDistributions(SecurityDistributionAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxPropertyStakes(PropertyStakeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxPrivateEquity(PrivateEquityAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxDeductions(DeductionAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxTargetAllocation(TargetAllocationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxCashConservation(CashConservationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxBonds(BondAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxBondValue(BondValueAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return JaxEngine()


class TestJaxFrozenRollout(FrozenRolloutAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxRentalIncome(RentalIncomeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxManagementFee(ManagementFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxRentalLifecycleCashflows(RentalLifecycleCashflowsAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxLeasingFee(LeasingFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxRentalIncomeTaxation(RentalIncomeTaxationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


class TestJaxRentalCashflowReconciliation(RentalCashflowReconciliationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_jax


if __name__ == "__main__":
    pytest_bazel.main()
