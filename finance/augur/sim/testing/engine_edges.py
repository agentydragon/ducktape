"""Month phases, edge inputs, and tax-loss harvesting, stated for any engine.

Three things that were once asserted only against one engine's own output layout: that each
phase of a month books what the scenario says when they all fire together; what a run does
with inputs it cannot use -- an unpriceable sleeve, an oversold lot, a channel carrying a
value that is not a price; and that harvesting a loss defers a gain rather than creating one,
which is the claim that makes the whole mechanism legitimate rather than free money.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    SP500_SYMBOL,
    IssuerId,
    LevelSeriesKey,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    FilingStatus,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.testing.case import Case, sampled
from finance.augur.sim.testing.simulation_result import Backend
from finance.augur.sim.tlh_harvest import HarvestYieldParams

# A high peak yield + strong drawdown sensitivity makes the harvested losses large enough to read
# cleanly off the YTD frame in a short horizon. These are test fixtures, not calibrated values.
_PARAMS = HarvestYieldParams(
    peak_annual_yield=0.12, floor_annual_yield=0.004, maturity_decay_exponent=1.5, drawdown_sensitivity=6.0
)


def _cash(run, agent_id: str, month_index: int) -> int:
    # `.item()` is typed Any; coerce so the lint aspect's mypy doesn't flag no-any-return.
    return int(
        run.cash.filter(
            (pl.col("agent_id") == agent_id) & (pl.col("month_index") == month_index) & (pl.col("rollout_index") == 0)
        )
        .get_column("balance_quanta")
        .item()
    )


def _gain(run, agent_id: str, classification: str, month_index: int) -> int:
    rows = run.capital_gains.filter(
        (pl.col("agent_id") == agent_id)
        & (pl.col("classification") == classification)
        & (pl.col("month_index") == month_index)
        & (pl.col("rollout_index") == 0)
    ).get_column("gain_quanta")
    return int(rows.item()) if len(rows) else 0


def _federal_tax(run) -> int:
    rows = run.tax_liabilities.filter(
        (pl.col("jurisdiction_id") == "federal_us") & (pl.col("rollout_index") == 0)
    ).get_column("amount_owed_quanta")
    return int(rows.sum())


def _external_series_context_for_levels(
    key: LevelSeriesKey, levels_by_rollout: list[list[float]]
) -> ExternalSeriesContext:
    return ExternalSeriesContext.from_level_blocks(
        [(key, np.asarray(levels_by_rollout, dtype=np.float64))],
        rollout_count=len(levels_by_rollout),
        horizon_months=len(levels_by_rollout[0]) - 1,
    )


def _pe_validation_scenario(*, horizon_months: int) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
        initial_lots=[
            InitialLot(
                lot_id="acme_lot",
                agent_id="alice",
                account_id="checking",
                asset=PrivateEquityAssetKey(issuer_id=IssuerId("acme")),
                purchase_month_index=-36,
                quantity=100.0,
                cost_basis_per_unit=10,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _pe_external_with_channel_value(
    *, channel: str, month: int, value: float, horizon_months: int
) -> ExternalSeriesContext:
    rollouts = 1
    shape = (rollouts, horizon_months + 1)
    tender = np.zeros(shape, dtype=np.bool_)
    valid = PrivateEquityBundle.from_issuer_arrays(
        "acme",
        mark_usd_per_unit=np.full(shape, 100.0, dtype=np.float64),
        regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
        event_kind_code=np.full(shape, int(PrivateEquityEventKindCode.NONE), dtype=np.int64),
        sale_opportunity_active=tender,
        sale_capacity_fraction=np.ones(shape, dtype=np.float64),
        eligible_fraction=np.ones(shape, dtype=np.float64),
        forced_sale_fraction=np.zeros(shape, dtype=np.float64),
        liquidity_blocked=np.zeros(shape, dtype=np.bool_),
        forced_recovery_cashout_usd=np.zeros(shape, dtype=np.float64),
        company_valuation_usd=np.zeros(shape, dtype=np.float64),
        rollout_count=rollouts,
        horizon_months=horizon_months,
    )
    patched = valid.frame.with_columns(
        pl.when((pl.col("rollout_index") == 0) & (pl.col("month_index") == month))
        .then(pl.lit(value, dtype=pl.Float64))
        .otherwise(pl.col(channel))
        .alias(channel)
    )
    return ExternalSeriesContext(private_equity=PrivateEquityBundle(patched))


def _sp500_levels(levels_by_rollout: list[list[float]]) -> ExternalSeriesContext:
    return ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=SP500_SYMBOL), np.asarray(levels_by_rollout, dtype=np.float64))],
        rollout_count=len(levels_by_rollout),
        horizon_months=len(levels_by_rollout[0]) - 1,
    )


def _harvest_scenario(
    *,
    horizon_months: int,
    quantity: float = 1000.0,
    cost_basis_per_unit: int = 1,
    purchase_month_index: int = 0,
    with_harvest: bool,
    short_term_fraction: float = 1.0,
    scheduled_asset_sales: list[ScheduledAssetSale] | None = None,
    extra_lots: list[InitialLot] | None = None,
) -> Scenario:
    """Single taxable agent holding an SP500 sleeve, optionally with a harvest policy.

    The sleeve is one lot priced by the `sp500` series; `unit_value`/quantity are chosen so MV is
    easy to reason about (1000 units at $1 cost basis). `extra_lots` adds non-sleeve lots (e.g. a
    gain lot to offset)."""

    lots = [
        InitialLot(
            lot_id="alice_sp500",
            agent_id="alice",
            account_id="brokerage",
            asset=SecurityKey(symbol=SP500_SYMBOL),
            purchase_month_index=purchase_month_index,
            quantity=quantity,
            cost_basis_per_unit=cost_basis_per_unit,
        )
    ]
    if extra_lots:
        lots.extend(extra_lots)
    harvest_policies = (
        [
            HarvestPolicy(
                owner_agent_id="alice",
                account_id="brokerage",
                asset=SecurityKey(symbol=SP500_SYMBOL),
                yield_params=_PARAMS,
                short_term_fraction=short_term_fraction,
            )
        ]
        if with_harvest
        else []
    )
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="brokerage", balance=0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
        ],
        initial_lots=lots,
        scheduled_asset_sales=scheduled_asset_sales or [],
        harvest_policies=harvest_policies,
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=horizon_months,
    )


def _ytd_gain(result, *, month_index: int, classification: str, rollout_index: int = 0) -> float:
    rows = result.capital_gains.filter(
        (pl.col("month_index") == month_index)
        & (pl.col("rollout_index") == rollout_index)
        & (pl.col("agent_id") == "alice")
        & (pl.col("classification") == classification)
    )
    if rows.is_empty():
        return 0.0
    return float(rows.get_column("gain_quanta").sum())


def _harvested_short_term_in_month(result, *, calendar_month: int, rollout_index: int = 0) -> float:
    """Magnitude of short-term loss harvested during `calendar_month` (a positive number).

    State snapshot `month_index = m + 1` reflects the end of calendar month `m`, so the loss booked
    during month `m` is the drop in cumulative YTD short-term gain from snapshot `m` to `m + 1`."""

    before = _ytd_gain(result, month_index=calendar_month, classification="stcg", rollout_index=rollout_index)
    after = _ytd_gain(result, month_index=calendar_month + 1, classification="stcg", rollout_index=rollout_index)
    return before - after


class ScanPhaseAcceptance:
    """Each month phase books what the scenario says, in a run that exercises them together."""

    def test_transfers_only_month_loop(self, backend: Backend) -> None:
        # Recurring paycheck for a year + a one-off gift: transfers and nothing else.
        scenario = Scenario(
            agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="bob")],
            initial_cash=[
                InitialAccountBalance(agent_id="payroll", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=100),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=500),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=11,
                    cause_id="paycheck",
                    from_agent_id="payroll",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=1000,
                )
            ],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=6,
                    cause_id="bob_gifts_alice",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=250,
                )
            ],
            tax_profiles=[],
            horizon_months=12,
        )
        run = backend(sampled(scenario, rollout_count=4, locations={}))

        # alice: 100 opening + 12 paychecks of 1000 + a 250 gift = 12350.
        assert _cash(run, "alice", 12) == 1_235_000
        assert _cash(run, "bob", 12) == 25_000
        assert _cash(run, "payroll", 12) == -1_200_000
        # Mid-horizon snapshot: 6 paychecks landed by month 6 (months 0..5), gift not yet (fires at 6).
        assert _cash(run, "alice", 6) == 610_000

    def test_configured_obligation_scan(self, backend: Backend) -> None:
        # Paycheck (transfer) + monthly rent (CONFIGURED obligation, settled via the funding/settlement
        # cores) — both phases the scan now folds. Always-funded, so no rollout fails.
        scenario = Scenario(
            agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="landlord")],
            initial_cash=[
                InitialAccountBalance(agent_id="payroll", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=1000),
                InitialAccountBalance(agent_id="landlord", account_id="checking", balance=0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=11,
                    cause_id="paycheck",
                    from_agent_id="payroll",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=5000,
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=11,
                    obligation_id="rent",
                    obligation_type="rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=2000,
                )
            ],
            tax_profiles=[],
            horizon_months=12,
        )
        run = backend(sampled(scenario, rollout_count=4, locations={}))

        # alice: 1000 opening + 12 paychecks of 5000 - 12 rents of 2000 = 37000.
        assert _cash(run, "alice", 12) == 3_700_000
        assert _cash(run, "landlord", 12) == 2_400_000
        assert _cash(run, "payroll", 12) == -6_000_000

    def test_obligation_failure_scan(self, backend: Backend) -> None:
        # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
        # rollout fails at month 1. Failure is per-rollout (a whole Monte-Carlo path), so
        # `_zero_failed_state` zeros every account in that rollout's column from the failure month on —
        # including the landlord's received rent. Exercises the scan's settlement failure path.
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=1000),
                InitialAccountBalance(agent_id="landlord", account_id="checking", balance=0),
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=11,
                    obligation_id="rent",
                    obligation_type="rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=600,
                )
            ],
            tax_profiles=[],
            horizon_months=12,
        )
        run = backend(sampled(scenario, rollout_count=4, locations={}))

        assert _cash(run, "alice", 1) == 40_000  # after month 0: rent paid (1000 -> 400)
        assert _cash(run, "landlord", 1) == 60_000  # month 0's rent landed pre-failure
        assert _cash(run, "alice", 12) == 0  # whole rollout zeroed after month-1 failure
        assert _cash(run, "landlord", 12) == 0  # landlord's column zeroed too

    def test_scheduled_sale_scan(self, backend: Backend, constant_price_bundle) -> None:
        # A long-term capital-gain sale: 100 SP500 units bought 24 months pre-horizon at $80, sold at
        # month 3 for $120 — FIFO lot matching, the proceeds credit, and the holding-period
        # classification, in one run. A flat price series keeps the assertion exact across rollouts.
        # The horizon ends before any December, so the profile is what makes the gain reportable
        # rather than what assesses it.
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
            ],
            initial_lots=[
                InitialLot(
                    lot_id="alice_sp500",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=SecurityKey(symbol=SP500_SYMBOL),
                    purchase_month_index=-24,  # long-term when sold at month 3
                    quantity=100.0,
                    cost_basis_per_unit=80,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=3,
                    cause_id="alice_sells_sp500",
                    agent_id="alice",
                    source_account_id="brokerage",
                    asset=SecurityKey(symbol=SP500_SYMBOL),
                    quantity=100.0,
                    proceeds_account_id="checking",
                )
            ],
            external_series=constant_price_bundle({SP500_SYMBOL: 120.0}),
            tax_profiles=[
                TaxProfile(
                    agent_id="alice",
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=6,
        )
        run = backend(sampled(scenario, rollout_count=4, locations={}))

        assert _cash(run, "alice", 3) == 0  # before the month-3 sale
        assert _cash(run, "alice", 4) == 1_200_000  # proceeds credited after month 3
        # Long-term realized gain = 100 * (120 - 80) = 4000, held in YTD through the (sub-year) horizon.
        assert _gain(run, "alice", "ltcg", 4) == 400_000
        assert _gain(run, "alice", "stcg", 4) == 0

    def test_cash_property_purchase_scan(self, backend: Backend) -> None:
        # All-cash (no-mortgage) home purchase at month 2: the buyer's down payment + closing cost moves
        # to the seller and the property goes active. No tax profile / property-tax policy / mortgage, so
        # it routes through the scan (the financed case is still barred). rented_fraction=0 -> no
        # depreciation, keeping the assertion to the cash move the fold performs.
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=2,
                    cause_id="alice_buys_home",
                    property_id="home",
                    location_id="sf",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=500000,  # all-cash
                    buyer_closing_cost=10000,
                    rented_fraction=0.0,
                )
            ],
            tax_profiles=[],
            horizon_months=6,
        )
        locations = {
            "sf": Location(
                location_id="sf",
                display_name="SF",
                jurisdiction_ids=["federal_us", "california"],
                annual_property_tax_rate=0.0118,
            )
        }
        run = backend(sampled(scenario, rollout_count=4, locations=locations))

        # stake = down payment + closing = 510k, moved buyer -> seller during month 2 (snapshot index 3).
        assert _cash(run, "alice", 2) == 60_000_000  # before purchase
        assert _cash(run, "alice", 3) == 9_000_000
        assert _cash(run, "seller", 3) == 51_000_000

    def test_property_tax_scan(self, backend: Backend) -> None:
        # Cash home purchase at month 0 + a property-tax policy (owner has no tax profile, so no SALT /
        # year-end pass): the monthly ad-valorem tax (assessed 500k × 1.2% / 12 = $500) is a PROPERTY_TAX
        # obligation the scan now accrues + settles, starting the month after purchase. Routes through the
        # scan (no tax profile, no mortgage).
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="county")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=600000),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="county", account_id="checking", balance=0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="alice_buys_home",
                    property_id="home",
                    location_id="sf",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=500000,
                    buyer_closing_cost=0,
                    rented_fraction=0.0,
                )
            ],
            property_tax_policies=[
                PropertyTaxPolicy(
                    property_id="home", owner_agent_id="alice", tax_authority_agent_id="county", annual_tax_rate=0.012
                )
            ],
            tax_profiles=[],
            horizon_months=4,
        )
        locations = {
            "sf": Location(
                location_id="sf",
                display_name="SF",
                jurisdiction_ids=["federal_us", "california"],
                annual_property_tax_rate=0.0118,
            )
        }
        run = backend(sampled(scenario, rollout_count=4, locations=locations))

        # After month 0: 500k purchase, no tax yet (accrues only once owned). Then $500/mo for months 1-3.
        assert _cash(run, "alice", 1) == 10_000_000
        assert _cash(run, "alice", 4) == 9_850_000
        assert _cash(run, "county", 4) == 150_000

    def test_financed_purchase_scan(self, backend: Backend) -> None:
        # A mortgage-financed home purchase: month 0 originates the loan (down payment moves buyer ->
        # seller, liability principal set), then monthly mortgage-payment obligations (interest/principal
        # split) settle buyer -> lender from month 1. No tax profile, so it routes through the scan.
        principal = 400_000
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="lender")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=300000),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="alice_buys_home",
                    property_id="home",
                    location_id="sf",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=100000,
                    buyer_closing_cost=0,
                    rented_fraction=0.0,
                    mortgage=MortgageFinancing(
                        liability_id="alice_mortgage",
                        lender_agent_id="lender",
                        principal=principal,
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                )
            ],
            tax_profiles=[],
            horizon_months=3,
        )
        locations = {
            "sf": Location(
                location_id="sf", display_name="SF", jurisdiction_ids=["federal_us"], annual_property_tax_rate=0.0118
            )
        }
        run = backend(sampled(scenario, rollout_count=4, locations=locations))

        # After month 0: down payment only (mortgage payments start the month after origination).
        assert _cash(run, "alice", 1) == 20_000_000
        # Months 1 & 2 each pay one mortgage bill to the lender; alice's cash nets both off.
        assert _cash(run, "lender", 3) == 479_640
        assert _cash(run, "alice", 3) == 19_520_360

    def test_year_end_tax_scan(self, backend: Backend) -> None:
        # Multi-year W-2 income + a tax profile with a prior-year tax: the December year-end pass accrues a
        # federal + CA liability, and the following year's estimated-tax + true-up obligations settle it.
        # Exercises the scan's full tax machinery (accrual + two-pass SALT + estimated/true-up settlement).
        scenario = Scenario(
            agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id="payroll", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=35,
                    cause_id="alice_paycheck",
                    from_agent_id="payroll",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(120000) / Decimal(12),
                    income_category=ORDINARY_INCOME,
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id="alice",
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us", "california"],
                    tax_authority_agent_id="irs",
                    prior_year_tax=15000,  # > 0 -> quarterly estimated-tax obligations next year
                )
            ],
            horizon_months=36,
        )
        run = backend(sampled(scenario, rollout_count=2, locations={}))
        federal_tax = _federal_tax(run)

        assert federal_tax > 0  # a real federal tax accrued at year-end
        assert _cash(run, "irs", 36) > 0  # estimated payments and true-ups reached the tax authority


class ValidationEdgeAcceptance:
    """What a run does at the edges: unusable inputs, terminal snapshots, and prices it cannot use."""

    @pytest.mark.parametrize(
        ("channel", "bad_value", "match"),
        [
            (
                "mark_usd_per_unit",
                -1.0,
                r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
            ),
            (
                "mark_usd_per_unit",
                float("nan"),
                r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
            ),
            (
                "forced_recovery_cashout_usd",
                -1.0,
                r"private-equity forced-recovery cashout series produced a negative value",
            ),
        ],
        ids=["pe-negative-mark", "pe-nonfinite-mark", "pe-negative-recovery"],
    )
    def test_private_equity_sampled_channel_validation(
        self, backend: Backend, channel: str, bad_value: float, match: str
    ) -> None:
        horizon = 2
        scenario = _pe_validation_scenario(horizon_months=horizon)
        external = _pe_external_with_channel_value(channel=channel, month=1, value=bad_value, horizon_months=horizon)

        with pytest.raises(ValueError, match=match):
            backend(Case(scenario=scenario, rollout_count=1, paths=external, locations={}))

    def test_a_private_equity_mark_is_required_at_the_terminal_snapshot_too(self, backend: Backend) -> None:
        """The last snapshot is not simulated, but it is read: it is where terminal value comes from.

        A mark that is not a mark there would be carried into the terminal portfolio rather
        than into a month's arithmetic, which is the quieter of the two failures and so the
        one worth refusing explicitly.
        """

        horizon = 2
        scenario = _pe_validation_scenario(horizon_months=horizon)
        external = _pe_external_with_channel_value(
            channel="mark_usd_per_unit", month=horizon, value=-1.0, horizon_months=horizon
        )

        with pytest.raises(ValueError, match=r"(?i)invalid mark value"):
            backend(Case(scenario=scenario, rollout_count=1, paths=external, locations={}))

    def test_a_security_price_is_required_at_the_terminal_snapshot_too(self, backend: Backend) -> None:
        """Same rule as the mark above, on the sleeve a harvest policy reads."""

        horizon = 2
        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
            ],
            initial_lots=[
                InitialLot(
                    lot_id="alice_sp500",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=SecurityKey(symbol=SP500_SYMBOL),
                    purchase_month_index=0,
                    quantity=100.0,
                    cost_basis_per_unit=1,
                )
            ],
            harvest_policies=[
                HarvestPolicy(
                    owner_agent_id="alice",
                    account_id="brokerage",
                    asset=SecurityKey(symbol=SP500_SYMBOL),
                    yield_params=HarvestYieldParams(
                        peak_annual_yield=0.12,
                        floor_annual_yield=0.004,
                        maturity_decay_exponent=1.5,
                        drawdown_sensitivity=6.0,
                    ),
                )
            ],
            tax_profiles=[
                TaxProfile(
                    agent_id="alice",
                    filing_status=FilingStatus.SINGLE,
                    jurisdiction_ids=["federal_us"],
                    tax_authority_agent_id="irs",
                )
            ],
            horizon_months=horizon,
        )
        external = _external_series_context_for_levels(SecurityKey(symbol=SP500_SYMBOL), [[1.0, 1.0, -1.0]])

        with pytest.raises(ValueError, match=r"(?i)non-positive value"):
            backend(Case(scenario=scenario, rollout_count=1, paths=external, locations={}))

    def test_scheduled_sale_oversell_validation(self, backend: Backend, constant_price_bundle) -> None:
        scenario = Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
            initial_lots=[
                InitialLot(
                    lot_id="taxable_vti",
                    agent_id="alice",
                    account_id="taxable",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    purchase_month_index=-12,
                    quantity=5.0,
                    cost_basis_per_unit=80,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=1,
                    cause_id="oversell",
                    agent_id="alice",
                    source_account_id="taxable",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    quantity=6.0,
                    proceeds_account_id="checking",
                )
            ],
            external_series=constant_price_bundle({SecuritySymbol("vti"): 100.0}),
            tax_profiles=[],
            horizon_months=2,
        )

        with pytest.raises(ValueError, match=r"(?i)(exceeds available lots|only .* are available)"):
            backend(sampled(scenario, rollout_count=1, locations={}))

    @pytest.mark.parametrize("bad_price", [0.0, -100.0, float("nan")], ids=["zero", "negative", "nonfinite"])
    def test_a_sleeve_price_that_is_not_a_price_is_refused(self, backend: Backend, bad_price: float) -> None:
        """Zero, negative and non-finite are all refused where the series is read.

        A sleeve the engine cannot price is not a sleeve worth nothing -- valuing it at zero
        would silently under-report net worth and under-fund the band, so the run stops at the
        series rather than carrying the number forward.
        """

        scenario = Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="landlord", account_id="checking", balance=0),
            ],
            initial_lots=[
                InitialLot(
                    lot_id="alice_vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    purchase_month_index=-24,
                    quantity=10.0,
                    cost_basis_per_unit=50,
                )
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=0,
                    obligation_id="rent_due",
                    obligation_type="rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=500,
                )
            ],
            target_allocation_policies=[
                TargetAllocationPolicy(
                    agent_id="alice",
                    account_id="checking",
                    sleeves=[SleeveTarget(asset=SecurityKey(symbol=SecuritySymbol("vti")), weight=1)],
                    cash_ceiling=0,
                )
            ],
            tax_profiles=[],
            horizon_months=1,
        )
        external = _external_series_context_for_levels(
            SecurityKey(symbol=SecuritySymbol("vti")), [[bad_price, bad_price]]
        )

        with pytest.raises(ValueError, match=r"(?i)(non-positive value|not finite)"):
            backend(Case(scenario=scenario, rollout_count=1, paths=external, locations={}))


class HarvestAcceptance:
    """Tax-loss harvesting defers a gain and gives it back; it never manufactures one."""

    @pytest.mark.parametrize("bad_level", [-0.01, float("nan")], ids=["negative", "nonfinite"])
    def test_harvest_index_validation_rejects_negative_or_nonfinite_prices(
        self, backend: Backend, bad_level: float
    ) -> None:
        scenario = _harvest_scenario(horizon_months=2, with_harvest=True)
        external_series = _sp500_levels([[1.0, bad_level, 1.0]])

        with pytest.raises(ValueError, match=r"(?i)(negative or non-finite price|non-positive value)"):
            backend(Case(scenario=scenario, rollout_count=1, paths=external_series, locations={}))

    def test_down_month_harvests_strictly_more_than_flat_month(self, backend: Backend) -> None:
        # Two rollouts, same fresh sleeve. Rollout 0 has a 20% drawdown in calendar month 1; rollout 1
        # is flat. The loss harvested DURING month 1 must be strictly larger for the drawdown rollout
        # (the drawdown kicker), isolating the period-return effect (both enter month 1 with the same
        # basis and a comparable embedded-gain fraction).
        scenario = _harvest_scenario(horizon_months=3, with_harvest=True)
        external_series = _sp500_levels([[1.0, 1.0, 0.8, 0.8], [1.0, 1.0, 1.0, 1.0]])
        result = backend(Case(scenario=scenario, rollout_count=2, paths=external_series, locations={}))

        drawdown_harvest = _harvested_short_term_in_month(result, calendar_month=2, rollout_index=0)
        flat_harvest = _harvested_short_term_in_month(result, calendar_month=2, rollout_index=1)
        assert drawdown_harvest > flat_harvest > 0.0

    def test_long_bull_run_ossifies_harvest_toward_floor(self, backend: Backend) -> None:
        # A long, steady bull run: basis stays at month-0 level while MV climbs, so the embedded-gain
        # fraction e -> 1 and the harvested loss per month decays toward the floor. Compare an early
        # month's harvest to a late month's; late must be strictly smaller (ossification).
        horizon = 24
        # +3%/month compounding bull market, no drawdowns.
        levels = [1.0 * (1.03**m) for m in range(horizon + 1)]
        scenario = _harvest_scenario(horizon_months=horizon, with_harvest=True)
        external_series = _sp500_levels([levels])
        result = backend(Case(scenario=scenario, rollout_count=1, paths=external_series, locations={}))

        early = _harvested_short_term_in_month(result, calendar_month=1)
        late = _harvested_short_term_in_month(result, calendar_month=12)
        assert early > 0.0  # a real loss was harvested early
        assert late < early  # ossification: harvest decays as embedded gains build

    def test_harvested_short_term_loss_offsets_realized_gain_lowering_tax(self, backend: Backend) -> None:
        # Alice realizes a real short-term capital GAIN (a separate crypto-like lot sold at a profit) in
        # the same year she harvests SP500 losses. With harvesting on, the harvested ST loss nets against
        # that gain (§1211/§1212), lowering the year's tax vs the no-harvest baseline.
        gain_lot = InitialLot(
            lot_id="alice_gain",
            agent_id="alice",
            account_id="brokerage",
            asset=SecurityKey(symbol=SecuritySymbol("gainco")),
            purchase_month_index=-3,  # short-term when sold at month 6
            quantity=100.0,
            cost_basis_per_unit=100,
        )
        gain_sale = ScheduledAssetSale(
            month=6,
            cause_id="alice_gain_sale",
            agent_id="alice",
            source_account_id="brokerage",
            asset=SecurityKey(symbol=SecuritySymbol("gainco")),
            quantity=100.0,
            proceeds_account_id="checking",
        )
        # SP500 sleeve drops then recovers so harvesting books meaningful losses through the year.
        sp500_levels = [1.0, 0.85, 0.85, 0.9, 0.9, 0.9, 0.95] + [0.95] * 7
        gain_levels = [400.0] * 14  # $400 against a $100 basis: a $30k short-term gain at the month-6 sale.
        external_series = ExternalSeriesContext.from_level_blocks(
            [
                (SecurityKey(symbol=SP500_SYMBOL), np.asarray([sp500_levels], dtype=np.float64)),
                (SecurityKey(symbol=SecuritySymbol("gainco")), np.asarray([gain_levels], dtype=np.float64)),
            ],
            rollout_count=1,
            horizon_months=len(sp500_levels) - 1,
        )

        def year_tax(with_harvest: bool) -> float:
            scenario = _harvest_scenario(
                horizon_months=13, with_harvest=with_harvest, scheduled_asset_sales=[gain_sale], extra_lots=[gain_lot]
            )
            result = backend(Case(scenario=scenario, rollout_count=1, paths=external_series, locations={}))
            accruals = result.events.tax_accruals.filter(pl.col("jurisdiction_id") == "federal_us")
            return float(accruals.get_column("amount_quanta").sum())

        tax_with = year_tax(with_harvest=True)
        tax_without = year_tax(with_harvest=False)
        assert tax_with < tax_without

    def test_give_back_makes_sale_gain_larger_by_cumulative_harvest_and_is_bounded(self, backend: Backend) -> None:
        # The deferral check. Hold the sleeve, harvest for several months, then liquidate the entire
        # sleeve. The realized gain at sale must be larger WITH harvesting than without — by exactly the
        # cumulative harvested loss booked over the held months — so the deferred gain is fully repaid.
        # Net: the year's total realized capital gain (harvested losses + give-back at sale) returns to
        # the no-harvest baseline, proving the benefit is deferral/timing, not unbounded free money.
        horizon = 8
        sale_month = 6
        # Flat sleeve price so the sale itself realizes ~zero economic gain; all the give-back is the
        # repaid deferral. (Price 1.0 == cost basis, so without harvest the sale gain is 0.)
        levels = [1.0] * (horizon + 1)
        sale = ScheduledAssetSale(
            month=sale_month,
            cause_id="alice_sp500_liquidate",
            agent_id="alice",
            source_account_id="brokerage",
            asset=SecurityKey(symbol=SP500_SYMBOL),
            quantity=1000.0,
            proceeds_account_id="checking",
        )
        external_series = _sp500_levels([levels])

        def run(with_harvest: bool):
            scenario = _harvest_scenario(
                horizon_months=horizon, with_harvest=with_harvest, scheduled_asset_sales=[sale]
            )
            return backend(Case(scenario=scenario, rollout_count=1, paths=external_series, locations={}))

        harvested = run(with_harvest=True)
        baseline = run(with_harvest=False)

        # Snapshot `month_index = m + 1` is the end of calendar month `m`. The sale fires inside month
        # `sale_month` (before that month's harvest, which then finds an empty sleeve), so the harvest
        # accumulated through the END of month sale_month-1 — i.e. snapshot `month_index = sale_month` —
        # is exactly what gets given back. It is the cumulative short-term loss booked so far (negative).
        cumulative_harvest = -_ytd_gain(harvested, month_index=sale_month, classification="stcg")
        assert cumulative_harvest > 0.0

        # Realized gain booked AT the sale = the jump in cumulative YTD across the sale month, i.e. from
        # snapshot `sale_month` to `sale_month + 1`.
        def sale_realized(result) -> float:
            before = _ytd_gain(result, month_index=sale_month, classification="stcg") + _ytd_gain(
                result, month_index=sale_month, classification="ltcg"
            )
            after = _ytd_gain(result, month_index=sale_month + 1, classification="stcg") + _ytd_gain(
                result, month_index=sale_month + 1, classification="ltcg"
            )
            return after - before

        # Baseline sale realizes ~0 (price == basis). The harvested run's sale realizes the give-back —
        # an extra gain equal to exactly the cumulative harvested loss (deferral repaid).
        assert sale_realized(baseline) == pytest.approx(0.0, abs=1e-6)
        assert sale_realized(harvested) == pytest.approx(cumulative_harvest, rel=1e-9, abs=1e-6)

        # Deferral, not free money: after the give-back, the net realized capital gain over the whole
        # (sub-year) horizon returns to the no-harvest baseline (~0) — bounded, not unbounded free money.
        net_st = _ytd_gain(harvested, month_index=horizon, classification="stcg")
        net_lt = _ytd_gain(harvested, month_index=horizon, classification="ltcg")
        assert net_st + net_lt == pytest.approx(0.0, abs=1e-6)

    def test_partial_sales_give_back_proportionally_and_never_exceed_harvest(self, backend: Backend) -> None:
        # Two partial sales (half, then the rest) must together give back exactly the cumulative harvest
        # — proportional to units sold — and never more (the scalar drains, so no double give-back).
        horizon = 9
        levels = [1.0] * (horizon + 1)
        sales = [
            ScheduledAssetSale(
                month=4,
                cause_id="alice_sp500_half",
                agent_id="alice",
                source_account_id="brokerage",
                asset=SecurityKey(symbol=SP500_SYMBOL),
                quantity=500.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=7,
                cause_id="alice_sp500_rest",
                agent_id="alice",
                source_account_id="brokerage",
                asset=SecurityKey(symbol=SP500_SYMBOL),
                quantity=500.0,
                proceeds_account_id="checking",
            ),
        ]
        scenario = _harvest_scenario(horizon_months=horizon, with_harvest=True, scheduled_asset_sales=sales)
        result = backend(Case(scenario=scenario, rollout_count=1, paths=_sp500_levels([levels]), locations={}))

        # By the terminal month, all units are sold, so the entire cumulative harvest has been given
        # back: the year-cumulative net short-term gain returns to ~0 (price flat == basis).
        net_st = _ytd_gain(result, month_index=horizon, classification="stcg")
        assert net_st == pytest.approx(0.0, abs=1e-6)

    def test_harvest_off_reproduces_baseline_capital_gains_exactly(self, backend: Backend) -> None:
        # Regression: a scenario with no harvest policy must produce byte-identical capital-gain YTD to
        # the same scenario run on the pre-harvest code path (here: no harvested losses ever appear).
        horizon = 6
        levels = [1.0, 0.8, 0.9, 0.85, 0.95, 1.1, 1.2]
        scenario = _harvest_scenario(horizon_months=horizon, with_harvest=False)
        result = backend(Case(scenario=scenario, rollout_count=1, paths=_sp500_levels([levels]), locations={}))
        # No sales, no harvest → no capital-gain rows at all.
        assert result.capital_gains.filter(pl.col("agent_id") == "alice").is_empty()
