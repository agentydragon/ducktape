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

from finance.augur.model.asset_key import PrivateEquityAssetKey
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
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    CashflowOnly,
    FilingStatus,
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


# A high peak yield + strong drawdown sensitivity makes the harvested losses large enough to read
# cleanly off the YTD frame in a short horizon. These are test fixtures, not calibrated values.
def _cash(run, agent_id: str, month_index: int) -> int:
    # `.item()` is typed Any; coerce so the lint aspect's mypy doesn't flag no-any-return.
    return int(
        run.cash.filter(
            (pl.col("agent_id") == agent_id) & (pl.col("month_index") == month_index) & (pl.col("rollout_id") == 0)
        )
        .get_column("balance_quanta")
        .item()
    )


def _gain(run, agent_id: str, classification: str, month_index: int) -> int:
    rows = run.capital_gains.filter(
        (pl.col("agent_id") == agent_id)
        & (pl.col("classification") == classification)
        & (pl.col("month_index") == month_index)
        & (pl.col("rollout_id") == 0)
    ).get_column("gain_quanta")
    return int(rows.item()) if len(rows) else 0


def _federal_tax(run) -> int:
    rows = run.tax_liabilities.filter(
        (pl.col("jurisdiction_id") == "federal_us") & (pl.col("rollout_id") == 0)
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
                cost_basis=1000,
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
        # rollout stops at month 1, preserving both parties' actual balances.
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
        assert _cash(run, "alice", 2) == 40_000
        assert _cash(run, "landlord", 2) == 60_000
        assert run.cash.get_column("month_index").max() == 2

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
                    cost_basis=8000,
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
                    cost_basis=400,
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
                    cost_basis=500,
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
                    allow_purchases=False,
                    rebalancing=CashflowOnly(),
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

        with pytest.raises(ValueError, match=r"(?i)(non-positive value|not finite|no finite level)"):
            backend(Case(scenario=scenario, rollout_count=1, paths=external, locations={}))
