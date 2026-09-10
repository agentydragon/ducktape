"""The Rust engine against the shared acceptance suites.

Nothing here but the name of the engine. What it has to satisfy is not particular to it and
lives in `sim/testing/`.

Stating the answer rather than comparing two engines is what outlived the second engine.
A comparison is blind to a rule both implementations get the same way and both get wrong;
these assertions say what the answer must be, so they still bind with one engine left.
"""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.rust.backend import RustEngine
from finance.augur.rust.result import run_rust
from finance.augur.sim.backend import Engine
from finance.augur.sim.testing.behaviour import (
    AssetSaleAcceptance,
    IndexedAmountAcceptance,
    ObligationAcceptance,
    PropertyCarryingCostAcceptance,
    RolloutFailureAcceptance,
    TransferAcceptance,
    YearEndTaxAcceptance,
)
from finance.augur.sim.testing.bonds import CPI_DOUBLING, bond_case
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.cash_conservation import CashConservationAcceptance
from finance.augur.sim.testing.deductions import DeductionAcceptance
from finance.augur.sim.testing.engine_acceptance import EngineAcceptance
from finance.augur.sim.testing.engine_edges import HarvestAcceptance, ScanPhaseAcceptance, ValidationEdgeAcceptance
from finance.augur.sim.testing.fixtures import SF, checking, home_purchase
from finance.augur.sim.testing.frozen_rollout import FrozenRolloutAcceptance
from finance.augur.sim.testing.income_sources import IncomeSourceAcceptance
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
from finance.augur.sim.testing.security_distributions import SecurityDistributionAcceptance
from finance.augur.sim.testing.simulation_result import Backend
from finance.augur.sim.testing.target_allocation import TargetAllocationAcceptance


class TestRustEngine(EngineAcceptance):
    @pytest.fixture
    def engine(self) -> Engine:
        return RustEngine()


class TestRustIncomeSources(IncomeSourceAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustSecurityDistributions(SecurityDistributionAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustPropertyStakes(PropertyStakeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustPrivateEquity(PrivateEquityAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustDeductions(DeductionAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustTargetAllocation(TargetAllocationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustCashConservation(CashConservationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


def test_product_net_worth_carries_indexed_bonds_at_indexed_principal() -> None:
    # Product action projection does not yet support held-bond histories. Keep this
    # actual product regression until that reader migrates under P12.
    indexed = bond_case(indexed=True, cpi=CPI_DOUBLING, is_taxed=False).compiled_run
    nominal = bond_case(indexed=False, cpi=CPI_DOUBLING, is_taxed=False).compiled_run
    engine = RustEngine()
    assert (
        engine.product_metrics(indexed, primary_agent_id="alice").metric_arrays()["bond_value_quanta"][-1, 0]
        == 200_000_000
    )
    assert (
        engine.product_metrics(nominal, primary_agent_id="alice").metric_arrays()["bond_value_quanta"][-1, 0]
        == 100_000_000
    )


class TestRustFrozenRollout(FrozenRolloutAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustRentalIncome(RentalIncomeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustManagementFee(ManagementFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustRentalLifecycleCashflows(RentalLifecycleCashflowsAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustLeasingFee(LeasingFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustRentalIncomeTaxation(RentalIncomeTaxationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustRentalCashflowReconciliation(RentalCashflowReconciliationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustTransfer(TransferAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustIndexedAmount(IndexedAmountAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustAssetSale(AssetSaleAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustYearEndTax(YearEndTaxAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustObligation(ObligationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustPropertyCarryingCost(PropertyCarryingCostAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustRolloutFailure(RolloutFailureAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustScanPhase(ScanPhaseAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustValidationEdge(ValidationEdgeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


class TestRustHarvest(HarvestAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_rust


def test_derived_building_basis_uses_engine_rounding() -> None:
    # The retired dense compiler incorrectly required this derived amount to be exact
    # cents. Authored money is exact; multiplication by the land share rounds in the engine.
    case = Case(
        scenario=scenario(
            checking(("alice", Decimal(200)), ("seller", Decimal(0))),
            scheduled_property_purchases=[
                home_purchase(
                    mortgage=None,
                    purchase_price=Decimal("100.01"),
                    down_payment=Decimal("100.01"),
                    buyer_closing_cost=Decimal(0),
                    land_value_fraction=0.2,
                )
            ],
            tax_profiles=[],
            horizon_months=1,
        ),
        rollout_count=1,
        locations={"sf": SF},
    )
    result = run_rust(case)
    assert result.property_details["building_basis_quanta"].to_list() == [8001]


if __name__ == "__main__":
    pytest_bazel.main()
