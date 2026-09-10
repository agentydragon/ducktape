"""Real monthly action sessions for the product's sales-only funding policy.

Tests use stipulated prices/indexes and, where stated, a synthetic flat tax schedule.
The configured product runner remains a separate cutover caller, not a fallback here.
"""

from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, LevelSeriesKey, RentKey, SecurityDistributionKey, SecurityKey
from finance.augur.product.funding import Policy
from finance.augur.product.scenarios import PRIMARY_ACCOUNT_ID, TAX_AUTHORITY_AGENT_ID, build_scenario
from finance.augur.product.wire import FundingPolicy, ScenarioKey, SleeveWeight, SpendIndex
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.results import Finished, Paid, RejectedAction, Rollout
from finance.augur.sim.scenario import (
    DistributionTaxSlice,
    FilingStatus,
    InitialLot,
    Scenario,
    SecurityDistribution,
    TaxProfile,
)
from finance.augur.sim.session import ActionSession

ACTOR = "test-owner"
FIRST = SecurityKey(symbol="test-first")
SECOND = SecurityKey(symbol="test-second")


def lot(id_: str, account: str, asset: SecurityKey, quantity: Decimal, month: int = -24) -> InitialLot:
    return InitialLot(
        lot_id=id_,
        agent_id=ACTOR,
        account_id=account,
        asset=asset,
        purchase_month_index=month,
        quantity=quantity,
        cost_basis=quantity * Decimal(50),
    )


def product_scenario(
    config: FundingPolicy,
    *,
    lots: tuple[InitialLot, ...],
    cash: Decimal = Decimal(0),
    spend: Decimal = Decimal(10),
    rent: Decimal = Decimal(0),
    spend_index: SpendIndex = SpendIndex.NONE,
    horizon: int = 1,
) -> Scenario:
    scenario = build_scenario(
        ScenarioKey(
            model_id="stipulated-policy-control",
            horizon_months=horizon,
            monthly_spend=spend,
            spend_index=spend_index,
            monthly_rent=rent,
            rental_location_id="test-location" if rent else None,
            funding_policy=config,
        ),
        primary_agent_id=ACTOR,
        initial_cash=cash,
        initial_lots=lots,
        properties_by_id={},
    )
    # Exercise product-authored claims with explicit Python decisions. Full service cutover
    # removes the configured policy at its real caller once housing/PE/harvest support lands.
    return scenario.model_copy(update={"target_allocation_policies": [], "tax_profiles": []})


def run(
    scenario: Scenario,
    config: FundingPolicy,
    series: dict[LevelSeriesKey, np.ndarray],
    *,
    jurisdictions: dict[str, Jurisdiction] | None = None,
) -> Rollout:
    prepared = compile_run(
        scenario,
        rollout_count=1,
        external_series=ExternalSeriesContext.from_level_blocks(
            list(series.items()), rollout_count=1, horizon_months=int(scenario.horizon_months)
        ),
        jurisdictions=jurisdictions or {},
        locations={},
    )
    policy = Policy(
        config,
        actor_id=ACTOR,
        cash_account_id=PRIMARY_ACCOUNT_ID,
        initial_lots=tuple(scenario.initial_lots),
        currency_quantum=scenario.currency.quantum,
    )
    session = ActionSession(prepared, ACTOR, [0])
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts[0]
    finally:
        session.close()


def test_symbol_weight_is_not_repeated_per_account_and_fifo_is_account_scoped() -> None:
    config = FundingPolicy(
        sleeve_weights=(SleeveWeight(symbol=FIRST.symbol, weight=1), SleeveWeight(symbol=SECOND.symbol, weight=1))
    )
    scenario = product_scenario(
        config,
        spend=Decimal(100),
        lots=(
            lot("first-new", "preferred", FIRST, Decimal("0.7"), -6),
            lot("first-old", "preferred", FIRST, Decimal("0.3"), -12),
            lot("globally-oldest", "later", FIRST, Decimal(1), -36),
            lot("second", "preferred", SECOND, Decimal(1)),
        ),
    )
    result = run(scenario, config, {asset: np.full((1, 2), 100.0) for asset in (FIRST, SECOND)})
    # FIRST totals $200 versus SECOND $100. A $100 withdrawal comes entirely from FIRST,
    # emptying the preferred account despite the older lot in the later account.
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("lot_id", "proceeds_quanta").rows() == [("first-old", 3000), ("first-new", 7000)]
    assert [row.action.kind for row in result.trace.receipts] == ["Sell", "PayClaim"]
    assert result.stop is None
    remaining = {row.lot_id: row.units_remaining for row in result.summary.ending_book.lots}
    assert remaining == {"first-new": 0, "first-old": 0, "globally-oldest": 1_000_000, "second": 1_000_000}


@pytest.mark.parametrize(("cash", "raised", "ending"), [(250, 40, 280), (270, 0, 260), (290, 0, 280), (400, 0, 390)])
def test_refill_to_ceiling_inclusive_band_and_surplus_never_invested(cash: int, raised: int, ending: int) -> None:
    config = FundingPolicy(
        cash_floor=260,
        cash_ceiling=280,
        cash_band_index_to_inflation=False,
        sleeve_weights=(SleeveWeight(symbol=FIRST.symbol, weight=1),),
    )
    scenario = product_scenario(config, cash=Decimal(cash), lots=(lot("fund", "brokerage", FIRST, Decimal(10)),))
    result = run(scenario, config, {FIRST: np.full((1, 2), 100.0)})
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.get_column("proceeds_quanta").sum() == raised * 100
    assert result.summary.cash[0].values == [cash * 100, ending * 100]
    assert {row.action.kind for row in result.trace.receipts} <= {"Sell", "PayClaim"}


@pytest.mark.parametrize(
    "weights", [(), (SleeveWeight(symbol=FIRST.symbol, weight=0),), (SleeveWeight(symbol="absent", weight=1),)]
)
def test_empty_excluded_or_unheld_targets_allow_cash_payments_but_never_sell(weights: tuple[SleeveWeight, ...]) -> None:
    # Indexed nonzero bounds still need no CPI when sales are disabled, matching app semantics.
    config = FundingPolicy(cash_floor=100, cash_ceiling=200, sleeve_weights=weights)
    scenario = product_scenario(
        config,
        cash=Decimal(50),
        spend=Decimal(30),
        rent=Decimal(40),
        horizon=2,
        lots=(lot("keep", "brokerage", FIRST, Decimal(10)),),
    )
    result = run(
        scenario, config, {FIRST: np.full((1, 3), 100.0), RentKey(location_id="test-location"): np.ones((1, 3))}
    )
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.is_empty()
    # Intentional ordered-action semantics: first $30 payment stays paid; the next $40 claim
    # fails with $20 left. The old configured group would reject both against their $70 total.
    assert result.stop == RejectedAction(month=0, action_index=1)
    assert result.summary.cash[0].values == [5000, 2000]
    assert [isinstance(row.receipt.outcome, Paid) for row in result.summary.payments] == [True, False]
    assert result.summary.ending_book.month == 1


def test_zero_weight_excludes_from_sales_and_target_denominator_even_on_exhaustion() -> None:
    config = FundingPolicy(
        sleeve_weights=(SleeveWeight(symbol=FIRST.symbol, weight=0), SleeveWeight(symbol=SECOND.symbol, weight=1))
    )
    scenario = product_scenario(
        config,
        spend=Decimal(150),
        lots=(lot("keep", "brokerage", FIRST, Decimal(10)), lot("sell", "brokerage", SECOND, Decimal(1))),
    )
    result = run(scenario, config, {asset: np.full((1, 2), 100.0) for asset in (FIRST, SECOND)})
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("lot_id", "proceeds_quanta").rows() == [("sell", 10_000)]
    assert result.stop == RejectedAction(month=0, action_index=1)
    assert result.summary.ending_book.lots[0].units_remaining == 10_000_000


def test_monthly_cpi_band_rounds_original_bound_once() -> None:
    config = FundingPolicy(
        cash_floor=Decimal("0.01"),
        cash_ceiling=Decimal("0.01"),
        sleeve_weights=(SleeveWeight(symbol=FIRST.symbol, weight=1),),
    )
    scenario = product_scenario(
        config, spend=Decimal("0.01"), horizon=3, lots=(lot("fund", "brokerage", FIRST, Decimal(10)),)
    )
    result = run(scenario, config, {FIRST: np.full((1, 4), 100.0), InflationKey(): np.array([[3.0, 4.0, 5.0, 99.0]])})
    assert result.summary.cash[0].values == [0, 1, 1, 2]
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.get_column("proceeds_quanta").to_list() == [2, 1, 2]
    with pytest.raises(ValueError, match="requires a supplied CPI"):
        run(scenario, config, {FIRST: np.full((1, 4), 100.0)})


def test_product_spend_tracks_monthly_cpi_but_rent_resets_only_annually() -> None:
    config = FundingPolicy()
    scenario = product_scenario(
        config,
        cash=Decimal(1000),
        spend=Decimal(1),
        rent=Decimal(10),
        spend_index=SpendIndex.INFLATION,
        horizon=14,
        lots=(),
    )
    cpi = np.full((1, 15), 1.5)
    cpi[:, 0] = 1
    cpi[:, 12] = 2
    cpi[:, 13:] = 3
    rent = np.full((1, 15), 9.0)
    rent[:, 0] = 1
    rent[:, 12] = 2
    rent[:, 13:] = 8
    result = run(scenario, config, {InflationKey(): cpi, RentKey(location_id="test-location"): rent})
    payments = result.summary.payments
    assert [
        row.receipt.amount_paid
        for row in payments
        if row.target is not None and row.target.obligation_type == "cash_spend"
    ] == [100] + [150] * 11 + [200, 300]
    assert [
        row.receipt.amount_paid
        for row in payments
        if row.target is not None and row.target.obligation_type == "outside_rent"
    ] == [1000] * 12 + [2000] * 2
    assert result.stop is None


def test_coupon_precedes_funding_and_next_year_tax_is_an_explicit_funded_claim() -> None:
    config = FundingPolicy(sleeve_weights=(SleeveWeight(symbol=FIRST.symbol, weight=1),))
    scenario = product_scenario(
        config, spend=Decimal(50), horizon=13, lots=(lot("fund", "brokerage", FIRST, Decimal(20)),)
    )
    rule = Jurisdiction(
        jurisdiction_id="test-flat",
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    scenario = scenario.model_copy(
        update={
            "tax_profiles": [
                TaxProfile(
                    agent_id=ACTOR,
                    jurisdiction_ids=[rule.jurisdiction_id],
                    tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
                    prior_year_tax=Decimal(0),
                )
            ],
            "security_distributions": [
                SecurityDistribution(
                    asset=FIRST,
                    agent_id=ACTOR,
                    holding_account_id="brokerage",
                    to_account_id=PRIMARY_ACCOUNT_ID,
                    tax_character=(DistributionTaxSlice(fraction=1),),
                )
            ],
        }
    )
    coupons = np.zeros((1, 14))
    coupons[:, 0] = 2
    result = run(
        scenario,
        config,
        {FIRST: np.full((1, 14), 100.0), SecurityDistributionKey(symbol=FIRST.symbol): coupons},
        jurisdictions={rule.jurisdiction_id: rule},
    )
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (1000, 500)
    # Year 1: $40 coupon × 20% + $280 realized LT gains × 10% = $36 tax.
    assert result.summary.tax_accruals[0].total_tax == 3600
    assert sum(row.amount_paid for row in result.summary.tax_payments) == 3600
    assert sales.select("month_index", "proceeds_quanta").row(-1) == (12, 8600)
    assert result.stop is None


if __name__ == "__main__":
    pytest_bazel.main()
