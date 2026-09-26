"""Real monthly action sessions for the product's sales-only funding policy.

Tests use stipulated prices/indexes and, where stated, a synthetic flat tax schedule.
The configured product runner remains a separate cutover caller, not a fallback here.
"""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import (
    InflationKey,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.product.funding import Policy
from finance.augur.product.scenarios import PRIMARY_ACCOUNT_ID, TAX_AUTHORITY_AGENT_ID, Situation, build_situation
from finance.augur.product.wire import FundingPolicy, ScenarioKey, SecuritySleeveWeight, SleeveWeight, SpendIndex
from finance.augur.sim.bills import Biller
from finance.augur.sim.compiler.execution import compile_holding_pools, compile_jurisdictions, compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LotId
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.results import Finished, Paid, RejectedAction, Rollout
from finance.augur.sim.scenario import (
    DistributionTaxSlice,
    FilingStatus,
    InitialLot,
    InterestIncome,
    SecurityDistribution,
    TaxProfile,
)
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

BROKERAGE = AccountId("brokerage")

ACTOR = AgentId("test-owner")
FIRST = SecurityKey(symbol=SecuritySymbol("test-first"))
SECOND = SecurityKey(symbol=SecuritySymbol("test-second"))


def lot(id_: LotId, account: AccountId, asset: SecurityKey, quantity: Decimal, month: int = -24) -> InitialLot:
    return InitialLot(
        lot_id=id_,
        agent_id=ACTOR,
        account_id=account,
        asset=asset,
        purchase_month_index=month,
        quantity=float(quantity),
        cost_basis=quantity * Decimal(50),
    )


@dataclass(frozen=True)
class Product:
    """The app's situation for one request, and the opening lots the Python policy reads."""

    situation: Situation
    lots: tuple[InitialLot, ...]
    distributions: tuple[SecurityDistribution, ...]


def product_situation(
    config: FundingPolicy,
    *,
    lots: tuple[InitialLot, ...],
    cash: Decimal = Decimal(0),
    spend: Decimal = Decimal(10),
    rent: Decimal = Decimal(0),
    spend_index: SpendIndex = SpendIndex.NONE,
    horizon: int = 1,
    distributions: tuple[SecurityDistribution, ...] = (),
) -> Product:
    situation = build_situation(
        ScenarioKey(
            model_id="stipulated-policy-control",
            horizon_months=horizon,
            monthly_spend=spend,
            spend_index=spend_index,
            monthly_rent=rent,
            rental_location_id=LocationId("test-location") if rent else None,
            funding_policy=config,
        ),
        primary_agent_id=ACTOR,
        initial_cash=cash,
        initial_lots=lots,
        properties_by_id={},
        locations={},
        security_distributions=distributions,
    )
    return Product(situation=situation, lots=lots, distributions=distributions)


def run(
    product: Product,
    config: FundingPolicy,
    series: dict[LevelSeriesKey, np.ndarray],
    *,
    tax: tuple[TaxProfile, Jurisdiction] | None = None,
) -> Rollout:
    """The product's accounts, holdings and claims with explicit Python decisions in place of its household.

    Untaxed unless `tax` names a profile and its rule; the configured funding policy is not consulted.
    """
    situation = product.situation
    jurisdictions = {} if tax is None else {tax[1].jurisdiction_id: tax[1]}
    world = World(
        MarketPath(
            compile_series(
                ExternalSeriesContext.from_level_blocks(
                    list(series.items()), rollout_count=1, horizon_months=situation.horizon_months
                ),
                rollout_count=1,
                horizon_months=situation.horizon_months,
                currency_quantum=situation.currency.quantum,
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=situation.horizon_months,
        income_sources=situation.income_sources,
        jurisdictions=compile_jurisdictions(jurisdictions, bonds=(), distributions=product.distributions),
    )
    for account in situation.accounts:
        world.declare_account(account)
    if tax is not None:
        world.track(TaxAuthority(compile_profile(tax[0], jurisdictions, quantum=situation.currency.quantum)))
    for pool in compile_holding_pools(lots=product.lots):
        world.declare_pool(pool)
    for held in situation.lots:
        world.hold(held)
    for distribution in situation.distributions:
        world.declare_distribution(distribution)
    for obligation in situation.obligations:
        world.track(Biller(obligation))
    policy = Policy(
        config,
        actor_id=ACTOR,
        cash_account_id=PRIMARY_ACCOUNT_ID,
        initial_lots=product.lots,
        currency_quantum=situation.currency.quantum,
    )
    session = ActionSession({0: world}, ACTOR)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts[0]
    finally:
        session.close()


def test_symbol_weight_is_not_repeated_per_account_and_fifo_is_account_scoped() -> None:
    config = FundingPolicy(
        sleeve_weights=(
            SecuritySleeveWeight(symbol=FIRST.symbol, weight=1),
            SecuritySleeveWeight(symbol=SECOND.symbol, weight=1),
        )
    )
    product = product_situation(
        config,
        spend=Decimal(100),
        lots=(
            lot(LotId("first-new"), AccountId("preferred"), FIRST, Decimal("0.7"), -6),
            lot(LotId("first-old"), AccountId("preferred"), FIRST, Decimal("0.3"), -12),
            lot(LotId("globally-oldest"), AccountId("later"), FIRST, Decimal(1), -36),
            lot(LotId("second"), AccountId("preferred"), SECOND, Decimal(1)),
        ),
    )
    result = run(product, config, {asset: np.full((1, 2), 100.0) for asset in (FIRST, SECOND)})
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
        cash_floor=Decimal(260),
        cash_ceiling=Decimal(280),
        cash_band_index_to_inflation=False,
        sleeve_weights=(SecuritySleeveWeight(symbol=FIRST.symbol, weight=1),),
    )
    product = product_situation(config, cash=Decimal(cash), lots=(lot(LotId("fund"), BROKERAGE, FIRST, Decimal(10)),))
    result = run(product, config, {FIRST: np.full((1, 2), 100.0)})
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.get_column("proceeds_quanta").sum() == raised * 100
    assert result.summary.cash[0].values == [cash * 100, ending * 100]
    assert {row.action.kind for row in result.trace.receipts} <= {"Sell", "PayClaim"}


@pytest.mark.parametrize(
    "weights",
    [
        (),
        (SecuritySleeveWeight(symbol=FIRST.symbol, weight=0),),
        (SecuritySleeveWeight(symbol=SecuritySymbol("absent"), weight=1),),
    ],
)
def test_empty_excluded_or_unheld_targets_allow_cash_payments_but_never_sell(weights: tuple[SleeveWeight, ...]) -> None:
    # Indexed nonzero bounds still need no CPI when sales are disabled, matching app semantics.
    config = FundingPolicy(cash_floor=Decimal(100), cash_ceiling=Decimal(200), sleeve_weights=weights)
    product = product_situation(
        config,
        cash=Decimal(50),
        spend=Decimal(30),
        rent=Decimal(40),
        horizon=2,
        lots=(lot(LotId("keep"), BROKERAGE, FIRST, Decimal(10)),),
    )
    result = run(
        product,
        config,
        {FIRST: np.full((1, 3), 100.0), RentKey(location_id=LocationId("test-location")): np.ones((1, 3))},
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
        sleeve_weights=(
            SecuritySleeveWeight(symbol=FIRST.symbol, weight=0),
            SecuritySleeveWeight(symbol=SECOND.symbol, weight=1),
        )
    )
    product = product_situation(
        config,
        spend=Decimal(150),
        lots=(lot(LotId("keep"), BROKERAGE, FIRST, Decimal(10)), lot(LotId("sell"), BROKERAGE, SECOND, Decimal(1))),
    )
    result = run(product, config, {asset: np.full((1, 2), 100.0) for asset in (FIRST, SECOND)})
    assert result.trace is not None
    sales = result.trace.events.lot_dispositions
    assert sales.select("lot_id", "proceeds_quanta").rows() == [("sell", 10_000)]
    assert result.stop == RejectedAction(month=0, action_index=1)
    assert result.summary.ending_book.lots[0].units_remaining == 10_000_000


def test_monthly_cpi_band_rounds_original_bound_once() -> None:
    config = FundingPolicy(
        cash_floor=Decimal("0.01"),
        cash_ceiling=Decimal("0.01"),
        sleeve_weights=(SecuritySleeveWeight(symbol=FIRST.symbol, weight=1),),
    )
    product = product_situation(
        config, spend=Decimal("0.01"), horizon=3, lots=(lot(LotId("fund"), BROKERAGE, FIRST, Decimal(10)),)
    )
    result = run(product, config, {FIRST: np.full((1, 4), 100.0), InflationKey(): np.array([[3.0, 4.0, 5.0, 99.0]])})
    assert result.summary.cash[0].values == [0, 1, 1, 2]
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.get_column("proceeds_quanta").to_list() == [2, 1, 2]
    with pytest.raises(ValueError, match="requires a supplied CPI"):
        run(product, config, {FIRST: np.full((1, 4), 100.0)})


def test_product_spend_tracks_monthly_cpi_but_rent_resets_only_annually() -> None:
    config = FundingPolicy()
    product = product_situation(
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
    result = run(product, config, {InflationKey(): cpi, RentKey(location_id=LocationId("test-location")): rent})
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
    config = FundingPolicy(sleeve_weights=(SecuritySleeveWeight(symbol=FIRST.symbol, weight=1),))
    rule = Jurisdiction(
        jurisdiction_id=JurisdictionId("test-flat"),
        level=JurisdictionLevel.FEDERAL,
        ordinary_income_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.20)]},
        ltcg_brackets={FilingStatus.SINGLE: [TaxBracket(upper="Infinity", rate=0.10)]},
        standard_deduction={FilingStatus.SINGLE: Decimal(0)},
        max_capital_loss_ordinary_offset={FilingStatus.SINGLE: Decimal(0)},
    )
    product = product_situation(
        config,
        spend=Decimal(50),
        horizon=13,
        lots=(lot(LotId("fund"), BROKERAGE, FIRST, Decimal(20)),),
        distributions=(
            SecurityDistribution(
                asset=FIRST,
                agent_id=ACTOR,
                holding_account_id=BROKERAGE,
                to_account_id=PRIMARY_ACCOUNT_ID,
                tax_character=(DistributionTaxSlice(fraction=1, income_category=InterestIncome()),),
            ),
        ),
    )
    profile = TaxProfile(
        agent_id=ACTOR,
        jurisdiction_ids=[rule.jurisdiction_id],
        tax_authority_agent_id=TAX_AUTHORITY_AGENT_ID,
        prior_year_tax=Decimal(0),
    )
    coupons = np.zeros((1, 14))
    coupons[:, 0] = 2
    result = run(
        product,
        config,
        {FIRST: np.full((1, 14), 100.0), SecurityDistributionKey(symbol=FIRST.symbol): coupons},
        tax=(profile, rule),
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
