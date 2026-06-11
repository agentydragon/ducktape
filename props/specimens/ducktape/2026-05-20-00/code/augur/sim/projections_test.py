from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from augur.sim.projections import project_simulation_run
from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    LiquidityPolicy,
    MortgageFinancing,
    PropertyTaxPolicy,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyPurchase,
    TaxProfile,
)
from augur.sim.simulate import simulate


def test_projection_due_now_obligation_sells_assets_and_settles(deterministic_market_bundle) -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
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
                amount_due_usd=500.0,
            )
        ],
        market=deterministic_market_bundle([100.0, 100.0]),
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"])],
        horizon_months=1,
    )

    projection = project_simulation_run(simulate(scenario, rollout_count=1))

    lifecycle = projection.obligation_lifecycle.row(0, named=True)
    assert lifecycle["obligation_id"] == "rent_due_m0"
    assert lifecycle["status"] == "paid"
    assert lifecycle["amount_due_usd"] == pytest.approx(500.0)
    assert lifecycle["amount_paid_usd"] == pytest.approx(500.0)
    assert lifecycle["shortfall_usd"] == pytest.approx(0.0)
    assert lifecycle["attempted_funding_sources"] == "vti"

    alice_final = _net_worth_row(projection.net_worth, agent_id="alice", month=1)
    assert alice_final["cash_usd"] == pytest.approx(0.0)
    assert alice_final["liquid_asset_value_usd"] == pytest.approx(600.0)
    assert alice_final["asset_book_value_usd"] == pytest.approx(300.0)
    assert alice_final["liquid_net_worth_usd"] == pytest.approx(600.0)
    assert alice_final["book_net_worth_usd"] == pytest.approx(300.0)

    transaction_types = set(projection.transactions.get_column("transaction_type").to_list())
    assert {"asset_sale", "cash_transfer", "obligation_settlement"} <= transaction_types
    sale = projection.transactions.filter(pl.col("transaction_type") == "asset_sale").row(0, named=True)
    assert sale["transaction_id"] == "liquidity_sale_m0_vti:alice_vti"
    assert sale["amount_usd"] == pytest.approx(400.0)
    assert sale["quantity"] == pytest.approx(4.0)

    summary = projection.rollout_summary.row(0, named=True)
    assert summary["status"] == "active"
    assert summary["failure_count"] == 0


def test_projection_due_now_obligation_failure_is_explicit() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
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
                amount_due_usd=500.0,
            )
        ],
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=[])],
        horizon_months=1,
    )

    projection = project_simulation_run(simulate(scenario, rollout_count=1))

    lifecycle = projection.obligation_lifecycle.row(0, named=True)
    assert lifecycle["status"] == "failed"
    assert lifecycle["amount_paid_usd"] == pytest.approx(0.0)
    assert lifecycle["shortfall_usd"] == pytest.approx(500.0)
    assert set(projection.transactions.get_column("transaction_type").to_list()) == {"obligation_settlement"}
    assert projection.transactions.get_column("amount_usd").to_list() == [0.0]

    failure = projection.failures.row(0, named=True)
    assert failure["failure_id"] == "rent_due_m0_failure"
    assert failure["obligation_id"] == "rent_due_m0"
    assert failure["obligation_type"] == "rent"
    assert failure["shortfall_usd"] == pytest.approx(500.0)

    summary = projection.rollout_summary.row(0, named=True)
    assert summary["status"] == "failed_insufficient_cash"
    assert summary["failed_month"] == 0
    assert summary["failure_count"] == 1
    assert summary["first_failure_month"] == 0


def test_projection_tax_safe_harbor_breakdown_and_payments() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_long_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="alice_long_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=100.0,
                price_per_unit_usd=280.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=4_000.0,
            )
        ],
        horizon_months=13,
    )

    projection = project_simulation_run(simulate(scenario, rollout_count=1))

    breakdowns = {
        row["jurisdiction_id"]: row for row in projection.tax_breakdowns.sort("jurisdiction_id").iter_rows(named=True)
    }
    assert breakdowns["federal_us"]["tax_year"] == 0
    assert breakdowns["federal_us"]["ordinary_taxable_usd"] == pytest.approx(35_400.0)
    assert breakdowns["federal_us"]["capital_gain_taxable_usd"] == pytest.approx(20_000.0)
    assert breakdowns["federal_us"]["ordinary_tax_usd"] == pytest.approx(4_016.0, abs=0.01)
    assert breakdowns["federal_us"]["capital_gain_tax_usd"] == pytest.approx(1_256.25, abs=0.01)
    assert breakdowns["california"]["total_tax_usd"] == pytest.approx(2_712.36, abs=0.01)

    paid_tax_obligations = projection.obligation_lifecycle.filter(
        pl.col("obligation_type").is_in(["estimated_tax", "tax_true_up"])
    ).sort(["month_index", "obligation_id"])
    assert paid_tax_obligations.select("month_index", "obligation_id", "amount_paid_usd", "status").to_dicts() == [
        {
            "month_index": 3,
            "obligation_id": "alice_estimated_tax_q1_y0",
            "amount_paid_usd": pytest.approx(1_000.0),
            "status": "paid",
        },
        {
            "month_index": 5,
            "obligation_id": "alice_estimated_tax_q2_y0",
            "amount_paid_usd": pytest.approx(1_000.0),
            "status": "paid",
        },
        {
            "month_index": 8,
            "obligation_id": "alice_estimated_tax_q3_y0",
            "amount_paid_usd": pytest.approx(1_000.0),
            "status": "paid",
        },
        {
            "month_index": 12,
            "obligation_id": "alice_estimated_tax_q4_y0",
            "amount_paid_usd": pytest.approx(1_000.0),
            "status": "paid",
        },
        {
            "month_index": 12,
            "obligation_id": "alice_tax_true_up_y0",
            "amount_paid_usd": pytest.approx(3_984.61, abs=0.02),
            "status": "paid",
        },
    ]

    alice_final = _net_worth_row(projection.net_worth, agent_id="alice", month=13)
    assert alice_final["cash_usd"] == pytest.approx(71_015.39, abs=0.02)
    assert alice_final["book_net_worth_usd"] == pytest.approx(71_015.39, abs=0.02)


def test_projection_real_estate_book_net_worth_and_liability_balance() -> None:
    scenario = Scenario(
        agents=[
            Agent(agent_id="alice"),
            Agent(agent_id="seller"),
            Agent(agent_id="bank"),
            Agent(agent_id="sf_tax_collector"),
        ],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=120_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="bank", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="sf_tax_collector", account_id="checking", balance_usd=0.0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="alice_buys_sf_home",
                property_id="sf_home",
                location_id="san_francisco",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price_usd=500_000.0,
                down_payment_usd=100_000.0,
                buyer_closing_cost_usd=10_000.0,
                mortgage=MortgageFinancing(
                    liability_id="sf_home_mortgage",
                    lender_agent_id="bank",
                    principal_usd=400_000.0,
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            )
        ],
        property_tax_policies=[
            PropertyTaxPolicy(
                property_id="sf_home",
                owner_agent_id="alice",
                tax_authority_agent_id="sf_tax_collector",
                annual_tax_rate=0.012,
            )
        ],
        horizon_months=2,
    )

    projection = project_simulation_run(simulate(scenario, rollout_count=1))

    mortgage_payment = 400_000.0 * 0.005 / (1.0 - (1.005**-360))
    expected_cash = 120_000.0 - 110_000.0 - mortgage_payment - 510.0
    expected_principal = 400_000.0 - (mortgage_payment - 2_000.0)
    alice_final = _net_worth_row(projection.net_worth, agent_id="alice", month=2)
    assert alice_final["cash_usd"] == pytest.approx(expected_cash)
    assert alice_final["property_book_value_usd"] == pytest.approx(510_000.0)
    assert alice_final["liability_principal_usd"] == pytest.approx(expected_principal)
    assert alice_final["book_net_worth_usd"] == pytest.approx(expected_cash + 510_000.0 - expected_principal)
    assert alice_final["liquid_net_worth_usd"] == pytest.approx(expected_cash)

    mortgage_account = projection.account_balances.filter(
        (pl.col("month_index") == 2) & (pl.col("account_id") == "sf_home_mortgage")
    ).row(0, named=True)
    assert mortgage_account["account_type"] == "liability"
    assert mortgage_account["balance_usd"] == pytest.approx(-expected_principal)

    obligation_types = set(projection.obligation_lifecycle.get_column("obligation_type").to_list())
    assert {"mortgage_payment", "property_tax"} <= obligation_types
    assert projection.failures.is_empty()


def test_projection_trajectory_filters_one_rollout() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=10.0)],
        horizon_months=1,
    )

    projection = project_simulation_run(simulate(scenario, rollout_count=2))
    trajectory = projection.trajectory(1)

    assert set(trajectory.net_worth.get_column("rollout_index").to_list()) == {1}
    assert set(trajectory.account_balances.get_column("rollout_index").to_list()) == {1}
    assert trajectory.rollout_summary.row(0, named=True)["rollout_index"] == 1


def _net_worth_row(frame: pl.DataFrame, *, agent_id: str, month: int) -> dict[str, object]:
    return frame.filter((pl.col("agent_id") == agent_id) & (pl.col("month_index") == month)).row(0, named=True)


if __name__ == "__main__":
    pytest_bazel.main()
