"""Configured financial execution against the legacy acceptance readers."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.testing.behaviour import PropertyCarryingCostAcceptance, YearEndTaxAcceptance
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.cash_conservation import CashConservationAcceptance
from finance.augur.sim.testing.configured_result import run_case
from finance.augur.sim.testing.deductions import DeductionAcceptance
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
from finance.augur.sim.testing.simulation_result import Backend
from finance.augur.sim.testing.target_allocation import TargetAllocationAcceptance


class TestConfiguredIncomeSources(IncomeSourceAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredPropertyStakes(PropertyStakeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredPrivateEquity(PrivateEquityAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredDeductions(DeductionAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredTargetAllocation(TargetAllocationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredCashConservation(CashConservationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredFrozenRollout(FrozenRolloutAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredRentalIncome(RentalIncomeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredManagementFee(ManagementFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredRentalLifecycleCashflows(RentalLifecycleCashflowsAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredLeasingFee(LeasingFeeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredRentalIncomeTaxation(RentalIncomeTaxationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredRentalCashflowReconciliation(RentalCashflowReconciliationAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredYearEndTax(YearEndTaxAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredPropertyCarryingCost(PropertyCarryingCostAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredScanPhase(ScanPhaseAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredValidationEdge(ValidationEdgeAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredHarvest(HarvestAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


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
    result = run_case(case)
    assert result.property_details["building_basis_quanta"].to_list() == [8001]


if __name__ == "__main__":
    pytest_bazel.main()
