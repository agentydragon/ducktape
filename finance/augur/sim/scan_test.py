"""Tests for the jitted `lax.scan` JAX engine (`run_jax_scan`).

The JAX backend is a single always-scan path — `run_jax_scan` compiles the whole month loop into one
`lax.scan`/XLA program covering every phase. These tests pin specific phases (transfers, obligations,
sales, purchases, property tax, mortgages, year-end tax) with exact expected values.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import SP500_SYMBOL, SecurityKey
from finance.augur.sim.locations import Location
from finance.augur.sim.runtime import mortgage_monthly_payment_usd
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate


def _cash(run, agent_id: str, month_index: int) -> float:
    # `.item()` is typed Any; coerce so the lint aspect's mypy doesn't flag no-any-return.
    return float(
        run.cash_balances.filter(
            (pl.col("agent_id") == agent_id) & (pl.col("month_index") == month_index) & (pl.col("rollout_index") == 0)
        )
        .get_column("balance_usd")
        .item()
    )


def test_transfers_only_scan() -> None:
    # Recurring paycheck for a year + a one-off gift: pure transfers, so JAX runs the lax.scan path.
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=500.0),
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
                amount_usd=1_000.0,
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
                amount_usd=250.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    # alice: 100 opening + 12 paychecks of 1000 + a 250 gift = 12350.
    assert _cash(run, "alice", 12) == pytest.approx(100.0 + 12 * 1_000.0 + 250.0)
    assert _cash(run, "bob", 12) == pytest.approx(500.0 - 250.0)
    assert _cash(run, "payroll", 12) == pytest.approx(-12 * 1_000.0)
    # Mid-horizon snapshot: 6 paychecks landed by month 6 (months 0..5), gift not yet (fires at 6).
    assert _cash(run, "alice", 6) == pytest.approx(100.0 + 6 * 1_000.0)


def test_configured_obligation_scan() -> None:
    # Paycheck (transfer) + monthly rent (CONFIGURED obligation, settled via the funding/settlement
    # cores) — both phases the scan now folds. Always-funded, so no rollout fails.
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
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
                amount_usd=5_000.0,
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
                amount_due_usd=2_000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    # alice: 1000 opening + 12 paychecks of 5000 - 12 rents of 2000 = 37000.
    assert _cash(run, "alice", 12) == pytest.approx(1_000.0 + 12 * 5_000.0 - 12 * 2_000.0)
    assert _cash(run, "landlord", 12) == pytest.approx(12 * 2_000.0)
    assert _cash(run, "payroll", 12) == pytest.approx(-12 * 5_000.0)


def test_obligation_failure_scan() -> None:
    # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
    # rollout fails at month 1. Failure is per-rollout (a whole Monte-Carlo path), so
    # `_zero_failed_state` zeros every account in that rollout's column from the failure month on —
    # including the landlord's received rent. Exercises the scan's settlement failure path.
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
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
                amount_due_usd=600.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    assert _cash(run, "alice", 1) == pytest.approx(400.0)  # after month 0: rent paid (1000 -> 400)
    assert _cash(run, "landlord", 1) == pytest.approx(600.0)  # month 0's rent landed pre-failure
    assert _cash(run, "alice", 12) == pytest.approx(0.0)  # whole rollout zeroed after month-1 failure
    assert _cash(run, "landlord", 12) == pytest.approx(0.0)  # landlord's column zeroed too


def _gain(run, agent_id: str, classification: str, month_index: int) -> float:
    rows = run.capital_gains_ytd.filter(
        (pl.col("agent_id") == agent_id)
        & (pl.col("classification") == classification)
        & (pl.col("month_index") == month_index)
        & (pl.col("rollout_index") == 0)
    ).get_column("gain_usd")
    return float(rows.item()) if len(rows) else 0.0


def test_scheduled_sale_scan() -> None:
    # A long-term capital-gain sale: 100 SP500 units bought 24 months pre-horizon at $80, sold at
    # month 3 for $120 — exercises the scan's FIFO lot matching, proceeds credit, and capital-gain
    # classification. No tax profiles, so the year-end pass never runs and the scenario routes through
    # the scan. Deterministic fixed price keeps the assertion exact across rollouts.
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="alice_sp500",
                agent_id="alice",
                account_id="brokerage",
                asset=SecurityKey(symbol=SP500_SYMBOL),
                purchase_month_index=-24,  # long-term when sold at month 3
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
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
                price_per_unit_usd=120.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=6,
    )
    run = simulate(scenario, rollout_count=4, locations={})

    assert _cash(run, "alice", 3) == pytest.approx(0.0)  # before the month-3 sale
    assert _cash(run, "alice", 4) == pytest.approx(100.0 * 120.0)  # proceeds credited after month 3
    # Long-term realized gain = 100 * (120 - 80) = 4000, held in YTD through the (sub-year) horizon.
    assert _gain(run, "alice", "ltcg", 4) == pytest.approx(4_000.0)
    assert _gain(run, "alice", "stcg", 4) == pytest.approx(0.0)


def test_cash_property_purchase_scan() -> None:
    # All-cash (no-mortgage) home purchase at month 2: the buyer's down payment + closing cost moves
    # to the seller and the property goes active. No tax profile / property-tax policy / mortgage, so
    # it routes through the scan (the financed case is still barred). rented_fraction=0 -> no
    # depreciation, keeping the assertion to the cash move the fold performs.
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=600_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
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
                purchase_price_usd=500_000.0,
                down_payment_usd=500_000.0,  # all-cash
                buyer_closing_cost_usd=10_000.0,
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
    run = simulate(scenario, rollout_count=4, locations=locations)

    # stake = down payment + closing = 510k, moved buyer -> seller during month 2 (snapshot index 3).
    assert _cash(run, "alice", 2) == pytest.approx(600_000.0)  # before purchase
    assert _cash(run, "alice", 3) == pytest.approx(600_000.0 - 510_000.0)
    assert _cash(run, "seller", 3) == pytest.approx(510_000.0)


def test_property_tax_scan() -> None:
    # Cash home purchase at month 0 + a property-tax policy (owner has no tax profile, so no SALT /
    # year-end pass): the monthly ad-valorem tax (assessed 500k × 1.2% / 12 = $500) is a PROPERTY_TAX
    # obligation the scan now accrues + settles, starting the month after purchase. Routes through the
    # scan (no tax profile, no mortgage).
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="county")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=600_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="county", account_id="checking", balance_usd=0.0),
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
                purchase_price_usd=500_000.0,
                down_payment_usd=500_000.0,
                buyer_closing_cost_usd=0.0,
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
    run = simulate(scenario, rollout_count=4, locations=locations)

    # After month 0: 500k purchase, no tax yet (accrues only once owned). Then $500/mo for months 1-3.
    assert _cash(run, "alice", 1) == pytest.approx(100_000.0)
    assert _cash(run, "alice", 4) == pytest.approx(100_000.0 - 3 * 500.0)
    assert _cash(run, "county", 4) == pytest.approx(3 * 500.0)


def test_financed_purchase_scan() -> None:
    # A mortgage-financed home purchase: month 0 originates the loan (down payment moves buyer ->
    # seller, liability principal set), then monthly mortgage-payment obligations (interest/principal
    # split) settle buyer -> lender from month 1. No tax profile, so it routes through the scan.
    principal = 400_000.0
    payment = mortgage_monthly_payment_usd(principal, 0.06, 360)
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="lender")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=300_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="lender", account_id="checking", balance_usd=0.0),
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
                purchase_price_usd=500_000.0,
                down_payment_usd=100_000.0,
                buyer_closing_cost_usd=0.0,
                rented_fraction=0.0,
                mortgage=MortgageFinancing(
                    liability_id="alice_mortgage",
                    lender_agent_id="lender",
                    principal_usd=principal,
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
    run = simulate(scenario, rollout_count=4, locations=locations)

    # After month 0: down payment only (mortgage payments start the month after origination).
    assert _cash(run, "alice", 1) == pytest.approx(300_000.0 - 100_000.0)
    # Months 1 & 2 each pay one mortgage bill to the lender; alice's cash nets both off.
    assert _cash(run, "lender", 3) == pytest.approx(2 * payment)
    assert _cash(run, "alice", 3) == pytest.approx(300_000.0 - 100_000.0 - 2 * payment)


def _federal_tax(run) -> float:
    rows = run.tax_liabilities.filter(
        (pl.col("jurisdiction_id") == "federal_us") & (pl.col("rollout_index") == 0)
    ).get_column("amount_owed_usd")
    return float(rows.sum())


def test_year_end_tax_scan() -> None:
    # Multi-year W-2 income + a tax profile with a prior-year tax: the December year-end pass accrues a
    # federal + CA liability, and the following year's estimated-tax + true-up obligations settle it.
    # Exercises the scan's full tax machinery (accrual + two-pass SALT + estimated/true-up settlement).
    scenario = Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
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
                amount_usd=120_000.0 / 12.0,
                income_category=ORDINARY_INCOME,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=15_000.0,  # > 0 -> quarterly estimated-tax obligations next year
            )
        ],
        horizon_months=36,
    )
    run = simulate(scenario, rollout_count=2, locations={})
    federal_tax = _federal_tax(run)

    assert federal_tax > 0.0  # a real federal tax accrued at year-end
    assert _cash(run, "irs", 36) > 0.0  # estimated payments and true-ups reached the tax authority


if __name__ == "__main__":
    pytest_bazel.main()
