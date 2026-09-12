"""Configured financial execution against the legacy acceptance readers."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.configured_result import run_case
from finance.augur.sim.testing.engine_edges import ScanPhaseAcceptance, ValidationEdgeAcceptance
from finance.augur.sim.testing.fixtures import SF, checking, home_purchase
from finance.augur.sim.testing.simulation_result import Backend


class TestConfiguredScanPhase(ScanPhaseAcceptance):
    @pytest.fixture
    def backend(self) -> Backend:
        return run_case


class TestConfiguredValidationEdge(ValidationEdgeAcceptance):
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
