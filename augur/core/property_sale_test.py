from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.local_regulation import LocalRegulation, LocationId, TaxRegime
from augur.core.market_bundle_test_support import constant_market_bundle
from augur.core.property_sale import empty_property_disposition_arrays, property_disposition_arrays
from augur.core.scenario_set import (
    Actor,
    ActorRole,
    NotRentedRentalPlan,
    PropertyAssumptions,
    PropertySaleEvent,
    PropertySelection,
    RentalMode,
    RentalPlan,
    Scenario,
    TaxFilingStatus,
    TaxProfile,
    TransactionCosts,
    WholePropertyRentalPlan,
)


def _local_regulation(**overrides: object) -> LocalRegulation:
    values = {
        "property_tax_regime": TaxRegime.CALIFORNIA_PROP13,
        "default_tax_regimes": (TaxRegime.CALIFORNIA_PROP13,),
        "property_tax_annual_pct": 1.2,
        "notes": "test",
    }
    values.update(overrides)
    return LocalRegulation(**values)


def sale_scenario(
    *,
    rental_plan: RentalPlan | None = None,
    property_assumptions: PropertyAssumptions | None = None,
    tax_profile: TaxProfile | None = None,
    transaction_costs: TransactionCosts | None = None,
) -> Scenario:
    return Scenario(
        scenario_id="sale",
        label="Sale",
        actors=(Actor(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        events=(PropertySaleEvent(event_id="sell", month_index=3, property_id="sf_ashton"),),
        property_selection=PropertySelection(
            property_id="sf_ashton", location_id=LocationId.SAN_FRANCISCO_CA, purchase_price_usd=100_000
        ),
        rental_plan=rental_plan or NotRentedRentalPlan(),
        property_assumptions=property_assumptions or PropertyAssumptions(),
        tax_profile=tax_profile or TaxProfile(filing_status=TaxFilingStatus.MARRIED_FILING_SEPARATELY),
        transaction_costs=transaction_costs or TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=5),
    )


def test_property_sale_combines_closing_costs_local_transfer_tax_and_debt_payoff() -> None:
    bundle = constant_market_bundle(home_path=(1.0, 1.0, 1.1, 1.2))
    sale = property_disposition_arrays(
        sale_scenario(),
        bundle,
        property_value_usd=100_000 * bundle.home_value_multipliers(LocationId.SAN_FRANCISCO_CA),
        mortgage_balance_usd=np.full((2, 4), 40_000.0),
        purchase_price_usd=100_000,
        local_regulation=_local_regulation(local_transfer_tax_pct=1),
    )

    np.testing.assert_allclose(sale.property_sale_gross_usd[:, 3], 120_000)
    np.testing.assert_allclose(sale.sale_closing_cost_usd[:, 3], 7_200)
    np.testing.assert_allclose(sale.property_sale_debt_payoff_usd[:, 3], 40_000)
    np.testing.assert_allclose(sale.property_sale_adjusted_basis_usd[:, 3], 100_000)
    np.testing.assert_allclose(sale.property_sale_capital_gain_usd[:, 3], 12_800)
    np.testing.assert_allclose(sale.taxable_property_capital_gain_usd[:, 3], 12_800)
    # Disposition reports pre-tax proceeds. Sale tax accrues through the engine's
    # annual-tax obligation path.
    np.testing.assert_allclose(sale.property_sale_net_proceeds_usd[:, 3], 120_000 - 7_200 - 40_000)
    np.testing.assert_allclose(sale.property_sale_tax_usd[:, 3], 0)
    np.testing.assert_allclose(sale.sale_settlement.net_proceeds_usd, sale.property_sale_net_proceeds_usd)
    np.testing.assert_allclose(sale.net_property_sale_cash_flow_usd, sale.property_sale_net_proceeds_usd)


def test_property_sale_recaptures_rental_depreciation_before_capital_gains() -> None:
    bundle = constant_market_bundle()
    sale = property_disposition_arrays(
        sale_scenario(
            rental_plan=WholePropertyRentalPlan(
                rental_mode=RentalMode.RENT_WHOLE_PROPERTY, start_month=1, end_month=3, monthly_rent_usd=0
            ),
            property_assumptions=PropertyAssumptions(depreciable_basis_pct=100),
            tax_profile=TaxProfile(filing_status=TaxFilingStatus.SINGLE),
            transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        ),
        bundle,
        property_value_usd=100_000 * bundle.home_value_multipliers(LocationId.SAN_FRANCISCO_CA),
        mortgage_balance_usd=np.zeros((2, 4), dtype="float64"),
        purchase_price_usd=100_000,
        local_regulation=_local_regulation(),
    )

    expected_depreciation = 100_000 / (27.5 * 12) * 3
    np.testing.assert_allclose(sale.depreciation_recapture_usd[:, 3], expected_depreciation)
    np.testing.assert_allclose(sale.property_sale_capital_gain_exclusion_usd[:, 3], 0, atol=1e-9)
    np.testing.assert_allclose(sale.taxable_property_capital_gain_usd[:, 3], 0)
    np.testing.assert_allclose(sale.taxable_property_gain_usd[:, 3], expected_depreciation)
    # Recapture rate is bracket-aware and applied by the engine's annual-tax path,
    # not by property_disposition_arrays itself.
    np.testing.assert_allclose(sale.property_sale_tax_usd[:, 3], 0)


def test_property_without_sale_event_returns_zero_sale_cash_flow() -> None:
    bundle = constant_market_bundle()
    scenario = sale_scenario().model_copy(update={"events": ()})
    sale = property_disposition_arrays(
        scenario,
        bundle,
        property_value_usd=100_000 * bundle.home_value_multipliers(LocationId.SAN_FRANCISCO_CA),
        mortgage_balance_usd=np.full((2, 4), 40_000.0),
        purchase_price_usd=100_000,
        local_regulation=_local_regulation(),
    )

    assert sale.sale_month is None
    np.testing.assert_allclose(sale.property_sale_gross_usd, 0)
    np.testing.assert_allclose(sale.property_sale_net_proceeds_usd, 0)
    np.testing.assert_allclose(sale.net_property_sale_cash_flow_usd, 0)


def test_no_property_sale_returns_zero_arrays_without_local_regulation() -> None:
    sale = empty_property_disposition_arrays(constant_market_bundle())

    np.testing.assert_allclose(sale.property_sale_gross_usd, 0)
    np.testing.assert_allclose(sale.property_sale_net_proceeds_usd, 0)
    assert sale.sale_month is None


if __name__ == "__main__":
    pytest_bazel.main()
