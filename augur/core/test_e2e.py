"""E2e tests that exercise the augur simulation core in isolation.

These tests construct scenarios programmatically, run them through
the core engine with a deterministic market provider, and assert on financial
outcomes — no webapp, no FastAPI, no config files. Each test spells out the
expected computation so the test itself documents what the simulator should
produce.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import pytest_bazel
from numpy.testing import assert_allclose
from pydantic import ValidationError

from augur.core.accounting import ChartAccountRole, JournalEntryType, LotAssetClass, PostingSide
from augur.core.api import ScenarioRun, simulate_set
from augur.core.local_regulation import LocationId
from augur.core.market_bundle_test_support import NoopMarketBundleProvider
from augur.core.scenario_set import (
    AccountBalance,
    AccountType,
    AccruePartnerEquityAction,
    Actor,
    ActorRole,
    AssetType,
    CheckingFloorSellPublicStockPolicy,
    Financing,
    FinancingMode,
    FixedAmountPrivateEquitySaleRule,
    GenericSp500StockPosition,
    InitialBalanceSheet,
    MarketPathObservation,
    MarketRequest,
    MonthlySpendAction,
    MonthlySpendDecision,
    MonthlySpendPolicy,
    PartnerContributionDecision,
    PartnerEquityAccrualPolicy,
    PayMortgageAction,
    PrivateEquityPosition,
    PrivateEquitySaleDecision,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySaleOpportunityObservation,
    PrivateEquitySalePolicy,
    PropertyAssumptions,
    PropertySaleBasisGainDetail,
    PropertySaleEvent,
    PropertySelection,
    RentalMode,
    ReportSpec,
    RolloutStatusType,
    Scenario,
    ScenarioSet,
    SellPrivateEquityAction,
    SellPublicStockDecision,
    SellSp500Action,
    SettlePropertySaleAction,
    TaxFilingStatus,
    TaxPaymentAllocationDetail,
    TaxPaymentTiming,
    TaxProfile,
    TransactionCosts,
    TransferPartnerContributionAction,
    WholePropertyRentalPlan,
)


def _run_scenario(
    scenario: Scenario,
    *,
    rollout_count: int = 1,
    horizon_months: int = 12,
    market_provider: NoopMarketBundleProvider | None = None,
) -> ScenarioRun:
    market_request = MarketRequest(
        market_model_id="e2e_noop", rollout_count=rollout_count, horizon_months=horizon_months, seed=0
    )
    scenario_set = ScenarioSet(
        scenario_set_id=f"{scenario.scenario_id}_set",
        title=f"{scenario.label} Set",
        market_request=market_request,
        scenarios=(scenario,),
    )
    run = simulate_set(scenario_set, market_provider=market_provider or NoopMarketBundleProvider())
    return run.scenario(scenario.scenario_id)


def _simple_actor() -> Actor:
    return Actor(actor_id="alpha", label="Alpha", role=ActorRole.PRIMARY_OWNER)


def _cash_only_scenario(*, cash_usd: float, scenario_id: str = "e2e") -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        label=scenario_id.replace("_", " ").title(),
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=cash_usd,
                ),
            )
        ),
    )


def _posting_matrix(
    result: ScenarioRun,
    *,
    role: ChartAccountRole,
    side: PostingSide,
    journal_entry_type: JournalEntryType | None = None,
) -> np.ndarray:
    matrix = np.zeros_like(result.matrix("cash_usd"), dtype="float64")
    journal_type_by_id = {entry.journal_entry_id: entry.journal_entry_type for entry in result.journal_entries()}
    for posting in result.postings(role=role, side=side):
        if journal_entry_type is not None and journal_type_by_id[posting.journal_entry_id] is not journal_entry_type:
            continue
        matrix[posting.rollout_index, posting.month_index] += posting.amount_usd
    return matrix


def _balance_snapshot_matrix(result: ScenarioRun, *, role: ChartAccountRole) -> np.ndarray:
    matrix = np.zeros_like(result.matrix("cash_usd"), dtype="float64")
    for snapshot in result.balance_snapshots(role=role):
        matrix[snapshot.rollout_index, snapshot.month_index] += snapshot.balance_usd
    return matrix


def _lot_disposition_matrix(result: ScenarioRun, *, asset_class: LotAssetClass, amount_field: str) -> np.ndarray:
    matrix = np.zeros_like(result.matrix("cash_usd"), dtype="float64")
    for disposition in result.lot_dispositions(asset_class=asset_class):
        matrix[disposition.rollout_index, disposition.month_index] += getattr(disposition, amount_field)
    return matrix


def _accounting_detail_matrix(result: ScenarioRun, detail_type: type[Any], amount_field: str) -> np.ndarray:
    matrix = np.zeros_like(result.matrix("cash_usd"), dtype="float64")
    for detail in result.accounting_details(detail_type):
        matrix[detail.rollout_index, detail.month_index] += getattr(detail, amount_field)
    return matrix


def test_report_spec_can_omit_monthly_columns_from_response() -> None:
    scenario = _cash_only_scenario(cash_usd=10_000, scenario_id="compact_report")
    scenario_set = ScenarioSet(
        scenario_set_id="compact_report_set",
        title="Compact Report Set",
        market_request=MarketRequest(market_model_id="e2e_noop", rollout_count=2, horizon_months=2, seed=0),
        report_spec=ReportSpec(include_monthly_columns=False),
        scenarios=(scenario,),
    )

    response = simulate_set(scenario_set, market_provider=NoopMarketBundleProvider()).to_response()
    result = response.scenario_results[0]

    assert response.report_spec.include_monthly_columns is False
    assert result.monthly_columns is None
    assert result.terminal_columns is not None
    assert result.terminal_columns.row_count == 2
    assert result.metric_fan_columns["net_worth_usd"].row_count == 3


def test_cash_only_no_activity_preserves_balance() -> None:
    """Agent holds $100k in checking, no property, no investments, no spending.
    Flat market. Cash should remain exactly $100k at every month."""
    scenario = _cash_only_scenario(cash_usd=100_000)
    result = _run_scenario(scenario, horizon_months=12)

    # Single rollout, 13 months (0..12)
    assert result.matrix("cash_usd").shape == (1, 13)
    assert_allclose(result.series("cash_usd"), 100_000)
    assert_allclose(result.matrix("generic_sp500_value_usd"), 0)
    assert_allclose(result.matrix("private_equity_value_usd"), 0)
    assert_allclose(result.matrix("property_value_usd"), 0)
    assert_allclose(result.series("net_worth_usd"), 100_000)
    assert {entry.journal_entry_type for entry in result.journal_entries()} == {JournalEntryType.OPENING_BALANCE}
    assert len(result.balance_snapshots(role=ChartAccountRole.CHECKING_CASH)) == 13
    market_path = result.rollout(0).market_observations(MarketPathObservation)
    assert len(market_path) == 13
    assert market_path[0].month_index == 0
    assert market_path[0].sp500_multiplier == 1.0
    assert result.rollout(0).market_observations(PrivateEquitySaleOpportunityObservation) == ()


def test_simulate_set_rejects_policy_with_unknown_actor_path() -> None:
    """The public API validates scenario references before entering the engine."""
    scenario = _cash_only_scenario(cash_usd=100_000).model_copy(
        update={
            "policies": (MonthlySpendPolicy(policy_id="living_expenses", actor_id="ghost", monthly_spend_usd=5_000),)
        }
    )

    with pytest.raises(ValueError, match=r"scenarios\[0\]\.policies\[0\]\.actor_id references unknown actor 'ghost'"):
        _run_scenario(scenario, horizon_months=12)


def test_simulate_set_rejects_partner_equity_policy_for_other_property() -> None:
    """Property-specific partner-equity policies must name the selected property."""
    scenario = Scenario(
        scenario_id="invalid_partner_property",
        label="Invalid Partner Property",
        actors=(_simple_actor(), Actor(actor_id="beta", label="Beta", role=ActorRole.EQUITY_BUILDING_OCCUPANT)),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.VALLEJO_CA, purchase_price_usd=100_000
        ),
        policies=(
            PartnerEquityAccrualPolicy(
                policy_id="partner_equity",
                actor_id="beta",
                property_id="other_property",
                base_monthly_payment_usd=1_000,
            ),
        ),
    )

    with pytest.raises(
        ValueError,
        match=r"scenarios\[0\]\.policies\[0\]\.property_id references 'other_property', "
        r"but scenario selects 'test_property'",
    ):
        _run_scenario(scenario, horizon_months=12)


def test_sp500_only_grows_with_market() -> None:
    """Agent holds $50k in SP500 (basis = $50k), no cash, no property.
    SP500 multiplier goes: 1.0, 1.1, 1.2, 1.3 (monthly, not annualized).
    After 3 months the SP500 position should be $50k * 1.3 = $65k."""
    scenario = Scenario(
        scenario_id="sp500_growth",
        label="SP500 Growth",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=50_000,
                ),
            )
        ),
    )
    sp500_path = (1.0, 1.1, 1.2, 1.3)
    result = _run_scenario(scenario, horizon_months=3, market_provider=NoopMarketBundleProvider(sp500_path=sp500_path))

    assert result.matrix("cash_usd").shape == (1, 4)
    assert_allclose(result.series("cash_usd"), 0)
    # SP500 value tracks multiplier: 50k * [1.0, 1.1, 1.2, 1.3]
    assert_allclose(result.series("generic_sp500_value_usd"), [50_000, 55_000, 60_000, 65_000])
    # Net worth = SP500 only
    assert_allclose(result.series("net_worth_usd"), [50_000, 55_000, 60_000, 65_000])


def test_cash_and_sp500_combined_net_worth() -> None:
    """Agent holds $30k cash + $70k SP500. Flat market (all 1.0).
    Net worth should be $100k at every month."""
    scenario = Scenario(
        scenario_id="mixed",
        label="Mixed",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=30_000
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=70_000,
                    cost_basis_usd=70_000,
                ),
            ),
        ),
    )
    result = _run_scenario(scenario, horizon_months=6)

    assert_allclose(result.series("cash_usd"), 30_000)
    assert_allclose(result.series("generic_sp500_value_usd"), 70_000)
    assert_allclose(result.series("net_worth_usd"), 100_000)


def test_monthly_spend_drains_cash() -> None:
    """Agent starts with $100k cash, spends $5k/month. Flat market.
    Month 0: $100k. Month 1: $95k. ... Month 12: $40k."""
    scenario = Scenario(
        scenario_id="spend_down",
        label="Spend Down",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            )
        ),
        policies=(MonthlySpendPolicy(policy_id="living_expenses", actor_id="alpha", monthly_spend_usd=5_000),),
    )
    result = _run_scenario(scenario, horizon_months=12)

    # Month 0: initial $100k (spend applies from month 1 onward)
    assert_allclose(result.series("cash_usd")[0], 100_000)
    # Month 1: 100k - 5k = 95k
    assert_allclose(result.series("cash_usd")[1], 95_000)
    # Month 6: 100k - 6*5k = 70k
    assert_allclose(result.series("cash_usd")[6], 70_000)
    # Month 12: 100k - 12*5k = 40k
    assert_allclose(result.terminal("cash_usd"), 40_000)
    # Verify spend array
    assert_allclose(result.series("monthly_spend_usd")[0], 0)
    assert_allclose(result.series("monthly_spend_usd")[1], 5_000)
    assert_allclose(result.series("monthly_spend_usd")[12], 5_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MONTHLY_LIVING_EXPENSE, side=PostingSide.DEBIT),
        result.matrix("monthly_spend_usd"),
    )
    # Verify actions recorded for each month 1..12
    spend_actions = result.actions(MonthlySpendAction)
    assert len(spend_actions) == 12
    assert spend_actions[0].amount_usd == 5_000
    assert spend_actions[0].month_index == 1
    spend_decisions = result.policy_decisions(MonthlySpendDecision)
    assert len(spend_decisions) == 12
    assert spend_decisions[0].amount_usd == 5_000
    assert spend_decisions[0].month_index == 1


def test_monthly_spend_records_each_rollout_and_month() -> None:
    """A spend policy emits an action for every rollout where spend applies."""
    scenario = Scenario(
        scenario_id="multi_rollout_spend",
        label="Multi Rollout Spend",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            )
        ),
        policies=(MonthlySpendPolicy(policy_id="living_expenses", actor_id="alpha", monthly_spend_usd=5_000),),
    )
    result = _run_scenario(scenario, rollout_count=2, horizon_months=2)

    assert_allclose(result.matrix("cash_usd")[:, 0], 100_000)
    assert_allclose(result.matrix("cash_usd")[:, 1], 95_000)
    assert_allclose(result.matrix("cash_usd")[:, 2], 90_000)
    assert_allclose(result.matrix("monthly_spend_usd")[:, 1:], 5_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MONTHLY_LIVING_EXPENSE, side=PostingSide.DEBIT),
        result.matrix("monthly_spend_usd"),
    )
    assert [
        (action.rollout_index, action.month_index, action.amount_usd) for action in result.actions(MonthlySpendAction)
    ] == [(0, 1, 5_000), (1, 1, 5_000), (0, 2, 5_000), (1, 2, 5_000)]


def test_fixed_rate_mortgage_amortizes_and_purchase_cash_outlay_posts_at_month_zero() -> None:
    """Agent buys a $500k property with 20% down and 30-year fixed financing at 6%.
    Month 0 records the down payment plus buy-side closing costs. Month 1
    mortgage interest and principal match the standard amortization formula."""
    scenario = Scenario(
        scenario_id="mortgage_amortization",
        label="Mortgage Amortization",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.SAN_FRANCISCO_CA, purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=2.5, closing_cost_sell_pct=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            )
        ),
    )

    result = _run_scenario(scenario, horizon_months=12)

    loan_amount = 400_000
    monthly_rate = 0.06 / 12
    payment = loan_amount * monthly_rate * (1 + monthly_rate) ** 360 / ((1 + monthly_rate) ** 360 - 1)
    expected_month_1_interest = 2_000
    expected_month_1_principal = payment - expected_month_1_interest
    rollout = result.rollout(0)
    assert_allclose(rollout.series("cash_usd")[0], -12_500)
    status = rollout.status()
    assert status.status == RolloutStatusType.CASH_NEGATIVE
    assert status.first_negative_cash_month_index == 0
    assert status.failed_obligation_count == 0
    assert status.unpaid_obligation_usd == 0
    assert_allclose(status.min_cash_usd, np.min(rollout.series("cash_usd")))
    assert rollout.scenario_run.rollout_status_summary().total_rollout_count == 1
    assert rollout.scenario_run.rollout_status_summary().counts_by_status == {
        RolloutStatusType.ACTIVE: 0,
        RolloutStatusType.CASH_NEGATIVE: 1,
    }
    assert_allclose(rollout.series("property_value_usd")[0], 500_000)
    assert_allclose(rollout.series("mortgage_balance_usd")[0], loan_amount)
    assert_allclose(rollout.series("mortgage_interest_usd")[1], expected_month_1_interest)
    assert_allclose(rollout.series("mortgage_principal_usd")[1], expected_month_1_principal)
    assert_allclose(rollout.series("mortgage_payment_usd")[1], payment)
    assert_allclose(rollout.series("mortgage_balance_usd")[1], loan_amount - expected_month_1_principal)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE, side=PostingSide.DEBIT),
        result.matrix("mortgage_interest_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.MORTGAGE_PAYABLE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
        ),
        result.matrix("mortgage_principal_usd"),
    )
    mortgage_payments = result.actions(PayMortgageAction)
    assert len(mortgage_payments) == 12
    assert mortgage_payments[0].month_index == 1
    assert mortgage_payments[0].actor_id == "alpha"
    assert mortgage_payments[0].policy_id == "mortgage_servicing"
    assert_allclose(mortgage_payments[0].mortgage_payment_usd, payment)
    assert_allclose(mortgage_payments[0].mortgage_interest_usd, expected_month_1_interest)
    assert_allclose(mortgage_payments[0].mortgage_principal_usd, expected_month_1_principal)
    assert_allclose(mortgage_payments[0].mortgage_balance_after_usd, loan_amount - expected_month_1_principal)


def test_partner_equity_accrual_records_contributions_and_claims() -> None:
    """A partner contribution policy acts like a housing-cost contribution program.

    The partner sends cash every occupied month. Contributions are applied to
    house costs first, and the portion covering mortgage principal increases the
    partner's equity claim.
    """
    purchase_price = 100_000
    down_payment_pct = 20
    down_payment = purchase_price * down_payment_pct / 100
    loan_amount = purchase_price - down_payment
    monthly_principal = loan_amount / (30 * 12)
    horizon_months = 60

    scenario = Scenario(
        scenario_id="partner_equity_accrual",
        label="Partner Equity Accrual",
        actors=(_simple_actor(), Actor(actor_id="beta", label="Beta", role=ActorRole.EQUITY_BUILDING_OCCUPANT)),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.VALLEJO_CA, purchase_price_usd=purchase_price
        ),
        financing=Financing(
            financing_mode=FinancingMode.FIXED_30, down_payment_pct=down_payment_pct, mortgage_rate_pct=0
        ),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=40_000
                ),
            )
        ),
        policies=(
            PartnerEquityAccrualPolicy(
                policy_id="partner_equity",
                actor_id="beta",
                property_id="test_property",
                base_monthly_payment_usd=1_000,
                occupied_months=horizon_months,
                grow_with_inflation=False,
            ),
        ),
    )

    result = _run_scenario(scenario, horizon_months=horizon_months)

    partner_principal_credit = monthly_principal * horizon_months
    expected_owner_ledger = down_payment
    expected_partner_ledger = partner_principal_credit
    expected_ownership_pct = expected_partner_ledger / (expected_owner_ledger + expected_partner_ledger)
    expected_terminal_mortgage_balance = loan_amount - partner_principal_credit
    expected_home_equity = purchase_price - expected_terminal_mortgage_balance
    rollout = result.rollout(0)

    assert_allclose(rollout.series("partner_contribution_usd")[0], 0)
    assert_allclose(rollout.series("partner_contribution_usd")[1:], 1_000)
    assert np.all(rollout.series("partner_unallocated_excess_usd")[1:] > 0)
    assert np.all(rollout.series("partner_house_costs_usd")[1:] > monthly_principal)
    assert_allclose(rollout.series("partner_principal_credit_usd")[1:], monthly_principal)
    assert_allclose(rollout.series("owner_principal_credit_usd")[1:], 0)
    assert_allclose(rollout.series("partner_equity_ledger_usd")[60], expected_partner_ledger)
    assert_allclose(rollout.series("owner_equity_ledger_usd")[60], expected_owner_ledger)
    assert_allclose(rollout.series("partner_ownership_pct")[60], expected_ownership_pct)
    assert_allclose(rollout.series("mortgage_balance_usd")[60], expected_terminal_mortgage_balance)
    assert_allclose(rollout.series("home_equity_usd")[60], expected_home_equity)
    assert_allclose(rollout.series("partner_home_equity_claim_usd")[60], expected_partner_ledger)
    assert_allclose(rollout.series("owner_home_equity_claim_usd")[60], expected_owner_ledger)
    assert_allclose(
        rollout.series("partner_home_equity_claim_usd")[60] + rollout.series("owner_home_equity_claim_usd")[60],
        expected_home_equity,
    )
    assert_allclose(rollout.series("cash_usd")[0], 20_000)
    assert_allclose(rollout.series("cash_usd")[60], 20_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER, side=PostingSide.CREDIT),
        result.matrix("partner_contribution_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_CONTRIBUTION_USED, side=PostingSide.DEBIT),
        result.matrix("partner_contribution_used_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_UNALLOCATED_CLAIM, side=PostingSide.DEBIT),
        result.matrix("partner_unallocated_excess_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT),
        result.matrix("partner_principal_credit_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.OWNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT),
        result.matrix("owner_principal_credit_usd"),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.PARTNER_EQUITY_LEDGER),
        result.matrix("partner_equity_ledger_usd"),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.OWNER_EQUITY_LEDGER),
        result.matrix("owner_equity_ledger_usd"),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM),
        result.matrix("partner_home_equity_claim_usd"),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.OWNER_HOME_EQUITY_CLAIM),
        result.matrix("owner_home_equity_claim_usd"),
    )
    account_by_id = {account.chart_account_id: account for account in result.chart_accounts()}
    partner_postings = [
        posting
        for posting in result.postings()
        if account_by_id[posting.chart_account_id].role
        in {
            ChartAccountRole.PARTNER_CONTRIBUTION_USED,
            ChartAccountRole.PARTNER_UNALLOCATED_CLAIM,
            ChartAccountRole.PARTNER_PRINCIPAL_CREDIT,
            ChartAccountRole.OWNER_PRINCIPAL_CREDIT,
        }
    ]
    assert {account_by_id[posting.chart_account_id].property_id for posting in partner_postings} == {"test_property"}
    assert (
        len(list(result.postings(role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT)))
        == horizon_months
    )
    assert all(
        account_by_id[posting.chart_account_id].counterparty_actor_id == "alpha"
        for posting in result.postings(role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT)
    )
    partner_snapshot_rows = [
        snapshot
        for snapshot in result.balance_snapshots()
        if account_by_id[snapshot.chart_account_id].role
        in {
            ChartAccountRole.PARTNER_EQUITY_LEDGER,
            ChartAccountRole.OWNER_EQUITY_LEDGER,
            ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM,
            ChartAccountRole.OWNER_HOME_EQUITY_CLAIM,
        }
    ]
    assert {account_by_id[snapshot.chart_account_id].property_id for snapshot in partner_snapshot_rows} == {
        "test_property"
    }

    transfers = result.actions(TransferPartnerContributionAction)
    assert len(transfers) == horizon_months
    assert transfers[0].month_index == 1
    assert transfers[-1].month_index == horizon_months
    assert all(action.actor_id == "beta" for action in transfers)
    assert all(action.recipient_actor_id == "alpha" for action in transfers)
    assert all(action.amount_usd == 1_000 for action in transfers)
    assert_allclose(
        [action.applied_to_house_costs_usd for action in transfers], rollout.series("partner_contribution_used_usd")[1:]
    )
    assert all(action.unallocated_amount_usd > 0 for action in transfers)
    contribution_decisions = result.policy_decisions(PartnerContributionDecision)
    assert len(contribution_decisions) == horizon_months
    assert contribution_decisions[0].actor_id == "beta"
    assert contribution_decisions[0].recipient_actor_id == "alpha"
    assert contribution_decisions[0].requested_amount_usd == 1_000

    mortgage_payments = result.actions(PayMortgageAction)
    assert len(mortgage_payments) == horizon_months
    assert mortgage_payments[0].actor_id == "alpha"
    assert_allclose(mortgage_payments[0].mortgage_principal_usd, monthly_principal)
    assert_allclose(mortgage_payments[-1].mortgage_balance_after_usd, expected_terminal_mortgage_balance)

    accruals = result.actions(AccruePartnerEquityAction)
    assert len(accruals) == horizon_months
    assert accruals[0].actor_id == "beta"
    assert accruals[0].beneficiary_actor_id == "beta"
    assert accruals[0].property_id == "test_property"
    assert_allclose(accruals[0].principal_credit_usd, monthly_principal)
    assert_allclose(accruals[-1].ownership_pct_after, expected_ownership_pct)
    assert_allclose(accruals[-1].home_equity_claim_usd_after, expected_partner_ledger)


def test_property_sale_records_capital_gains_tax_and_net_proceeds() -> None:
    """A property sale records closing costs, the primary-residence exclusion,
    capital-gains tax, and net proceeds as part of the simulated cash-flow
    truth."""
    scenario = Scenario(
        scenario_id="property_sale_tax",
        label="Property Sale Tax",
        actors=(_simple_actor(),),
        events=(PropertySaleEvent(event_id="sale", month_index=60, property_id="test_property"),),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.SAN_FRANCISCO_CA, purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.CASH),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=6.5),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=500_000,
                ),
            )
        ),
    )
    sale_value = 900_000
    result = _run_scenario(
        scenario,
        horizon_months=60,
        market_provider=NoopMarketBundleProvider(home_path=tuple(np.linspace(1.0, sale_value / 500_000, 61))),
    )

    sale_closing_cost = sale_value * 0.065
    realized_gain = sale_value - sale_closing_cost - 500_000
    taxable_gain = realized_gain - 250_000
    federal_taxable_gain = taxable_gain - 16_100
    federal_sale_tax = (federal_taxable_gain - 49_450) * 0.15
    california_taxable_gain = taxable_gain - 5_706
    california_sale_tax = 3_201.97 + (california_taxable_gain - 72_724) * 0.093
    sale_tax = federal_sale_tax + california_sale_tax
    rollout = result.rollout(0)
    assert_allclose(rollout.series("property_sale_gross_usd")[60], sale_value)
    assert_allclose(rollout.series("sale_closing_cost_usd")[60], sale_closing_cost)
    assert_allclose(rollout.series("realized_property_gain_usd")[60], realized_gain)
    assert_allclose(rollout.series("property_sale_capital_gain_usd")[60], realized_gain)
    assert_allclose(rollout.series("property_sale_capital_gain_exclusion_usd")[60], 250_000)
    assert_allclose(rollout.series("taxable_property_capital_gain_usd")[60], taxable_gain)
    assert_allclose(rollout.series("taxable_property_gain_usd")[60], taxable_gain)
    assert_allclose(rollout.series("property_sale_tax_usd")[60], sale_tax)
    assert_allclose(rollout.series("federal_income_tax_usd")[60], federal_sale_tax)
    assert_allclose(rollout.series("california_income_tax_usd")[60], california_sale_tax)
    assert_allclose(rollout.series("property_sale_net_proceeds_usd")[60], sale_value - sale_closing_cost - sale_tax)
    assert_allclose(result.matrix("net_property_sale_cash_flow_usd"), result.matrix("property_sale_net_proceeds_usd"))
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PROPERTY,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix("property_sale_gross_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix("sale_closing_cost_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.TAX_EXPENSE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix("property_sale_tax_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        )
        - _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix("property_sale_net_proceeds_usd"),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "adjusted_basis_usd"),
        result.matrix("property_sale_adjusted_basis_usd"),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "realized_gain_usd"),
        result.matrix("realized_property_gain_usd"),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "taxable_gain_usd"),
        result.matrix("taxable_property_gain_usd"),
    )
    assert_allclose(
        _accounting_detail_matrix(result, TaxPaymentAllocationDetail, "federal_income_tax_usd"),
        result.matrix("federal_income_tax_usd"),
    )
    assert_allclose(
        _accounting_detail_matrix(result, TaxPaymentAllocationDetail, "california_income_tax_usd"),
        result.matrix("california_income_tax_usd"),
    )
    tax_details = rollout.accounting_details(TaxPaymentAllocationDetail)
    assert len(tax_details) == 1
    assert tax_details[0].payment_timing == TaxPaymentTiming.ALLOCATED_TO_SOURCE_MONTH
    assert_allclose(tax_details[0].property_sale_tax_usd, sale_tax)
    sale_accounting_details = rollout.accounting_details(PropertySaleBasisGainDetail)
    assert len(sale_accounting_details) == 1
    assert_allclose(sale_accounting_details[0].adjusted_basis_usd, 500_000)
    assert_allclose(sale_accounting_details[0].taxable_gain_usd, taxable_gain)
    actions = rollout.actions(SettlePropertySaleAction)
    assert len(actions) == 1
    action = actions[0]
    assert action.event_id == "sale"
    assert action.property_id == "test_property"
    assert action.policy_id == "property_sale_settlement"
    assert_allclose(action.gross_sale_usd, sale_value)
    assert_allclose(action.selling_cost_usd, sale_closing_cost)
    assert_allclose(action.debt_payoff_usd, 0)
    assert_allclose(action.adjusted_basis_usd, 500_000)
    assert_allclose(action.realized_gain_usd, realized_gain)
    assert_allclose(action.capital_gain_exclusion_usd, 250_000)
    assert_allclose(action.taxable_gain_usd, taxable_gain)
    assert_allclose(action.tax_usd, sale_tax)
    assert_allclose(action.net_proceeds_usd, sale_value - sale_closing_cost - sale_tax)


def test_partner_sale_claim_uses_settlement_net_proceeds() -> None:
    """A partner sale claim is allocated from actual sale proceeds, not unsold home equity."""
    purchase_price = 100_000
    sale_month = 3
    scenario = Scenario(
        scenario_id="partner_sale_claim",
        label="Partner Sale Claim",
        actors=(_simple_actor(), Actor(actor_id="beta", label="Beta", role=ActorRole.EQUITY_BUILDING_OCCUPANT)),
        events=(PropertySaleEvent(event_id="sale", month_index=sale_month, property_id="test_property"),),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.VALLEJO_CA, purchase_price_usd=purchase_price
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=0),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=10),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
        tax_profile=TaxProfile(filing_status=TaxFilingStatus.MARRIED_FILING_SEPARATELY),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=40_000
                ),
            )
        ),
        policies=(
            PartnerEquityAccrualPolicy(
                policy_id="partner_equity",
                actor_id="beta",
                property_id="test_property",
                base_monthly_payment_usd=1_000,
                occupied_months=sale_month,
                grow_with_inflation=False,
            ),
        ),
    )
    result = _run_scenario(
        scenario, horizon_months=4, market_provider=NoopMarketBundleProvider(home_path=(1.0, 1.0, 1.0, 2.0, 2.5))
    )

    rollout = result.rollout(0)
    sale_action = rollout.actions(SettlePropertySaleAction)[0]
    sale_net_proceeds = sale_action.net_proceeds_usd
    ownership_pct = rollout.series("partner_ownership_pct")[sale_month]
    expected_partner_claim = sale_net_proceeds * ownership_pct
    expected_owner_claim = sale_net_proceeds - expected_partner_claim
    gross_equity_claim = rollout.series("home_equity_usd")[sale_month] * ownership_pct

    assert_allclose(rollout.series("property_sale_net_proceeds_usd")[sale_month], sale_net_proceeds)
    assert_allclose(rollout.series("property_sale_debt_payoff_usd")[sale_month], sale_action.debt_payoff_usd)
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.MORTGAGE_PAYABLE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix("property_sale_debt_payoff_usd"),
    )
    assert sale_net_proceeds < rollout.series("home_equity_usd")[sale_month]
    assert not np.isclose(expected_partner_claim, gross_equity_claim)
    assert_allclose(rollout.series("partner_home_equity_claim_usd")[sale_month], expected_partner_claim)
    assert_allclose(rollout.series("owner_home_equity_claim_usd")[sale_month], expected_owner_claim)
    assert_allclose(
        rollout.series("partner_home_equity_claim_usd")[sale_month]
        + rollout.series("owner_home_equity_claim_usd")[sale_month],
        sale_net_proceeds,
    )
    assert_allclose(rollout.series("partner_home_equity_claim_usd")[4], expected_partner_claim)
    assert_allclose(rollout.series("owner_home_equity_claim_usd")[4], expected_owner_claim)

    sale_month_accruals = [
        action for action in rollout.actions(AccruePartnerEquityAction) if action.month_index == sale_month
    ]
    assert len(sale_month_accruals) == 1
    assert_allclose(sale_month_accruals[0].home_equity_claim_usd_after, expected_partner_claim)


def test_simulate_set_response_serializes_sale_actions_with_tax_detail() -> None:
    """The public response payload preserves per-rollout action details for UI inspection."""
    scenario = Scenario(
        scenario_id="serialized_sale_actions",
        label="Serialized Sale Actions",
        actors=(_simple_actor(),),
        events=(PropertySaleEvent(event_id="property_sale", month_index=2, property_id="test_property"),),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.SAN_FRANCISCO_CA, purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.CASH),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=6.5),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=550_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=25_000,
                ),
                PrivateEquityPosition(
                    asset_id="pe",
                    asset_type=AssetType.PRIVATE_EQUITY,
                    owner_actor_id="alpha",
                    value_usd=100_000,
                    cost_basis_usd=40_000,
                    units=100,
                ),
            ),
        ),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=60_000, sale_amount_usd=20_000
            ),
            PrivateEquitySalePolicy(
                policy_id="private_equity_sale",
                actor_id="alpha",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=50_000),
            ),
        ),
    )
    scenario_set = ScenarioSet(
        scenario_set_id="serialized_sale_actions_set",
        title="Serialized Sale Actions Set",
        market_request=MarketRequest(market_model_id="e2e_noop", rollout_count=1, horizon_months=2, seed=0),
        scenarios=(scenario,),
    )

    run = simulate_set(
        scenario_set,
        market_provider=NoopMarketBundleProvider(
            home_path=(1.0, 1.0, 1.8), private_equity_sale_opportunity_months=(1,)
        ),
    )
    payload = run.to_response().model_dump(mode="json")

    result = payload["scenario_results"][0]
    assert {"federal_income_tax_usd", "california_income_tax_usd", "generic_sp500_sale_tax_usd"} <= set(
        result["monthly_columns"]["columns"]
    )
    actions = {action["action_type"]: action for action in result["actions"]}
    assert set(actions) == {"sell_sp500", "sell_private_equity", "settle_property_sale"}
    account_by_id = {account["chart_account_id"]: account for account in result["chart_accounts"]}
    posting_roles = {account_by_id[posting["chart_account_id"]]["role"] for posting in result["postings"]}
    assert {"public_security", "private_equity", "checking_cash", "tax_expense"} <= posting_roles
    assert {"sell_public_stock", "private_equity_sale"} <= {
        decision["decision_type"] for decision in result["policy_decisions"]
    }
    assert {"market_path", "private_equity_sale_opportunity"} <= {
        observation["observation_type"] for observation in result["market_observations"]
    }
    assert {"property_sale_basis_gain", "tax_payment_allocation"} <= {
        detail["detail_type"] for detail in result["accounting_details"]
    }

    sp500_action = actions["sell_sp500"]
    assert sp500_action["amount_usd"] == 20_000
    assert sp500_action["basis_usd"] == 10_000
    assert sp500_action["gain_usd"] == 10_000
    assert sp500_action["tax_usd"] > 0
    assert_allclose(sp500_action["after_tax_proceeds_usd"], sp500_action["amount_usd"] - sp500_action["tax_usd"])

    private_equity_action = actions["sell_private_equity"]
    assert private_equity_action["event_id"] is None
    assert private_equity_action["event_type"] is None
    assert private_equity_action["opportunity_id"] == (
        f"{private_equity_action['path_set_id']}:path:0:month:1:private_equity_holding:pe:sale_opportunity"
    )
    assert private_equity_action["opportunity_cause_id"] == private_equity_action["opportunity_id"]
    assert private_equity_action["amount_usd"] == 50_000
    assert private_equity_action["basis_usd"] == 20_000
    assert private_equity_action["taxable_gain_usd"] == 30_000
    assert private_equity_action["estimated_tax_usd"] > 0
    assert_allclose(
        private_equity_action["after_tax_proceeds_usd"],
        private_equity_action["amount_usd"] - private_equity_action["estimated_tax_usd"],
    )

    property_action = actions["settle_property_sale"]
    assert property_action["event_id"] == "property_sale"
    assert property_action["property_id"] == "test_property"
    assert property_action["gross_sale_usd"] == 900_000
    assert property_action["selling_cost_usd"] == 58_500
    assert property_action["adjusted_basis_usd"] == 500_000
    assert property_action["taxable_capital_gain_usd"] == 91_500
    assert property_action["tax_usd"] > 0
    assert_allclose(
        property_action["net_proceeds_usd"],
        property_action["gross_sale_usd"]
        - property_action["selling_cost_usd"]
        - property_action["debt_payoff_usd"]
        - property_action["tax_usd"],
    )


def test_whole_property_rental_posts_income_fees_and_cash_flow() -> None:
    """A rented property records rent, vacancy, management fee, carrying cost,
    and owner cash impact in the simulated trajectory."""
    scenario = Scenario(
        scenario_id="whole_property_rental",
        label="Whole Property Rental",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id=LocationId.VALLEJO_CA, purchase_price_usd=120_000
        ),
        financing=Financing(financing_mode=FinancingMode.CASH),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
        rental_plan=WholePropertyRentalPlan(
            rental_mode=RentalMode.RENT_WHOLE_PROPERTY,
            start_month=1,
            monthly_rent_usd=3_000,
            vacancy_pct=5,
            management_fee_pct=8,
        ),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=250_000,
                ),
            )
        ),
    )

    result = _run_scenario(scenario, horizon_months=3)

    expected_rental_income = 3_000 * (1 - 0.05)
    expected_management_fee = expected_rental_income * 0.08
    expected_property_tax = 120_000 * 0.011 / 12
    expected_net_property_cash_flow = expected_rental_income - expected_management_fee - expected_property_tax
    rollout = result.rollout(0)
    assert_allclose(rollout.series("rental_income_usd")[0], 0)
    assert_allclose(rollout.series("rental_income_usd")[1], expected_rental_income)
    assert_allclose(rollout.series("rental_management_fee_usd")[1], expected_management_fee)
    assert_allclose(rollout.series("property_tax_usd")[1], expected_property_tax)
    assert_allclose(rollout.series("property_carrying_cost_usd")[1], expected_management_fee + expected_property_tax)
    assert_allclose(rollout.series("net_property_cash_flow_usd")[1], expected_net_property_cash_flow)
    assert_allclose(rollout.series("cash_usd")[0], 130_000)
    # Positive net rental income produces a CA ordinary-income tax obligation, which
    # the annual-tax pipeline settles in the same source month. Cash at month 1
    # therefore reflects the net rental cash flow minus the rental tax share.
    rental_tax_month_1 = rollout.series("rental_income_tax_usd")[1]
    assert rental_tax_month_1 > 0
    assert_allclose(rollout.series("cash_usd")[1], 130_000 + expected_net_property_cash_flow - rental_tax_month_1)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.RENTAL_INCOME, side=PostingSide.CREDIT),
        result.matrix("rental_income_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_OPERATING,
        ),
        result.matrix("rental_income_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PROPERTY_TAX_EXPENSE, side=PostingSide.DEBIT),
        result.matrix("property_tax_usd"),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.RENTAL_MANAGEMENT_FEE_EXPENSE, side=PostingSide.DEBIT),
        result.matrix("rental_management_fee_usd"),
    )


def test_pydantic_rejects_rental_mode_without_required_rent() -> None:
    """Rental configuration should not silently mean zero rent when rent is missing."""
    with pytest.raises(ValidationError, match=r"rental_plan\.rent_whole_property\.monthly_rent_usd"):
        Scenario.model_validate(
            {
                "scenario_id": "missing_rent",
                "label": "Missing Rent",
                "actors": [{"actor_id": "alpha", "label": "Alpha", "role": "primary_owner"}],
                "property_selection": {
                    "property_id": "test_property",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 120_000,
                },
                "rental_plan": {"rental_mode": "rent_whole_property"},
            }
        )


def test_checking_floor_policy_sells_sp500_to_restore_cash_floor() -> None:
    """A checking-floor rule can sell SP500 after monthly spend drains cash.

    This keeps the public API focused on a distribution result while still
    making a selected rollout's action log and curves inspectable.
    """
    scenario = Scenario(
        scenario_id="checking_floor_sale",
        label="Checking Floor Sale",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=30_000
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=25_000,
                ),
            ),
        ),
        policies=(
            MonthlySpendPolicy(policy_id="living_expenses", actor_id="alpha", monthly_spend_usd=5_000),
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=10_000, sale_amount_usd=20_000
            ),
        ),
    )

    result = _run_scenario(scenario, horizon_months=6)

    rollout = result.rollout(0)
    expected_stock_sale_tax = 42.94
    assert_allclose(
        rollout.series("cash_usd"),
        [30_000, 25_000, 20_000, 15_000, 10_000, 25_000 - expected_stock_sale_tax, 20_000 - expected_stock_sale_tax],
    )
    assert_allclose(rollout.series("generic_sp500_value_usd"), [50_000, 50_000, 50_000, 50_000, 50_000, 30_000, 30_000])
    assert_allclose(rollout.series("generic_sp500_sale_usd"), [0, 0, 0, 0, 0, 20_000, 0])
    assert_allclose(rollout.series("generic_sp500_sale_basis_usd")[5], 10_000)
    assert_allclose(rollout.series("generic_sp500_sale_gain_usd")[5], 10_000)
    assert_allclose(rollout.series("generic_sp500_sale_tax_usd")[5], expected_stock_sale_tax)
    assert_allclose(rollout.series("checking_floor_shortfall_usd"), 0)
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PUBLIC_SECURITY,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        ),
        result.matrix("generic_sp500_sale_usd"),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PUBLIC_SECURITY, amount_field="cost_basis_usd"),
        result.matrix("generic_sp500_sale_basis_usd"),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PUBLIC_SECURITY, amount_field="tax_expense_usd"),
        result.matrix("generic_sp500_sale_tax_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        )
        - result.matrix("generic_sp500_sale_tax_usd"),
        result.matrix("generic_sp500_sale_usd") - result.matrix("generic_sp500_sale_tax_usd"),
    )

    actions = result.actions(SellSp500Action)
    assert len(actions) == 1
    assert actions[0].month_index == 5
    assert actions[0].policy_id == "checking_floor"
    assert actions[0].amount_usd == 20_000
    assert_allclose(actions[0].after_tax_proceeds_usd, 20_000 - expected_stock_sale_tax)
    assert actions[0].basis_usd == 10_000
    assert actions[0].gain_usd == 10_000
    assert_allclose(actions[0].tax_usd, expected_stock_sale_tax)
    assert actions[0].shortfall_usd == 0
    decisions = result.policy_decisions(SellPublicStockDecision)
    assert len(decisions) == 1
    assert decisions[0].month_index == 5
    assert decisions[0].policy_id == "checking_floor"
    assert decisions[0].requested_amount_usd == 20_000
    assert decisions[0].current_cash_usd == 5_000
    assert decisions[0].target_cash_floor_usd == 10_000


def test_multiple_checking_floor_rules_execute_in_policy_order() -> None:
    """Checking-floor rules are ordered policy rules, not a singleton archetype."""
    scenario = Scenario(
        scenario_id="ordered_checking_floor_rules",
        label="Ordered Checking Floor Rules",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=0
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=50_000,
                ),
            ),
        ),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="primary_floor", actor_id="alpha", floor_usd=10_000, sale_amount_usd=15_000
            ),
            CheckingFloorSellPublicStockPolicy(
                policy_id="top_up_floor", actor_id="alpha", floor_usd=20_000, sale_amount_usd=10_000
            ),
        ),
    )

    result = _run_scenario(scenario, horizon_months=1)

    rollout = result.rollout(0)
    assert_allclose(rollout.series("cash_usd")[0], 25_000)
    assert_allclose(rollout.series("generic_sp500_value_usd")[0], 25_000)
    assert_allclose(rollout.series("generic_sp500_sale_usd")[0], 25_000)
    assert_allclose(rollout.series("generic_sp500_sale_basis_usd")[0], 25_000)
    assert_allclose(rollout.series("checking_floor_shortfall_usd")[0], 0)

    assert [(action.policy_id, action.amount_usd) for action in result.actions(SellSp500Action)] == [
        ("primary_floor", 15_000),
        ("top_up_floor", 10_000),
    ]


def test_private_equity_tender_sale_into_cash_increases_only_actual_liquid_assets() -> None:
    """A tender sale into cash changes liquidity only by the after-tax sale proceeds."""
    scenario = Scenario(
        scenario_id="private_equity_tender_sale",
        label="Private Equity Tender Sale",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=10_000
                ),
            ),
            assets=(
                PrivateEquityPosition(
                    asset_id="pe",
                    asset_type=AssetType.PRIVATE_EQUITY,
                    owner_actor_id="alpha",
                    value_usd=200_000,
                    cost_basis_usd=80_000,
                    units=100,
                ),
            ),
        ),
        policies=(
            PrivateEquitySalePolicy(
                policy_id="private_equity_sale",
                actor_id="alpha",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=100_000),
            ),
        ),
    )

    no_opportunity = _run_scenario(scenario, horizon_months=12)
    assert_allclose(no_opportunity.rollout(0).series("private_equity_sale_usd"), 0)
    assert_allclose(no_opportunity.rollout(0).series("cash_usd")[12], 10_000)
    assert_allclose(no_opportunity.rollout(0).series("liquid_net_worth_usd")[12], 10_000)
    assert no_opportunity.actions(SellPrivateEquityAction) == ()
    no_opportunity_decisions = no_opportunity.policy_decisions(PrivateEquitySaleDecision)
    assert len(no_opportunity_decisions) == 13
    assert {decision.decision_reason for decision in no_opportunity_decisions} == {
        PrivateEquitySaleDecisionReason.NO_SALE_OPPORTUNITY
    }
    assert no_opportunity_decisions[-1].requested_amount_usd == 0
    assert no_opportunity_decisions[-1].sale_opportunity_value_usd == 0
    assert no_opportunity_decisions[-1].opportunity_id is None
    assert no_opportunity_decisions[-1].opportunity_cause_id == (
        f"{no_opportunity_decisions[-1].path_set_id}:path:0:month:12:private_equity_holding:pe:no_sale_opportunity"
    )
    assert no_opportunity.rollout(0).market_observations(PrivateEquitySaleOpportunityObservation) == ()

    result = _run_scenario(
        scenario,
        horizon_months=12,
        market_provider=NoopMarketBundleProvider(private_equity_sale_opportunity_months=(12,)),
    )

    expected_sale = 100_000
    expected_basis = 40_000
    expected_taxable_gain = 60_000
    expected_tax = 1_792.53
    expected_after_tax_proceeds = expected_sale - expected_tax
    rollout = result.rollout(0)
    assert_allclose(rollout.series("private_equity_value_usd")[11], 200_000)
    assert_allclose(rollout.series("private_equity_sale_usd")[12], expected_sale)
    assert_allclose(rollout.series("private_equity_sale_basis_usd")[12], expected_basis)
    assert_allclose(rollout.series("private_equity_sale_tax_usd")[12], expected_tax)
    assert_allclose(rollout.series("private_equity_value_usd")[12], 100_000)
    assert_allclose(rollout.series("private_equity_sale_opportunity_value_usd")[12], 100_000)
    assert_allclose(rollout.series("cash_usd")[12], 10_000 + expected_after_tax_proceeds)
    assert_allclose(rollout.series("liquid_net_worth_usd")[12], 10_000 + expected_after_tax_proceeds)
    assert_allclose(rollout.series("net_worth_usd")[12], 10_000 + expected_after_tax_proceeds + 100_000)
    opportunity_observations = rollout.market_observations(PrivateEquitySaleOpportunityObservation)
    assert len(opportunity_observations) == 1
    assert opportunity_observations[0].month_index == 12
    expected_opportunity_id = (
        f"{opportunity_observations[0].path_set_id}:path:0:month:12:private_equity_holding:pe:sale_opportunity"
    )
    assert opportunity_observations[0].opportunity_id == expected_opportunity_id
    assert opportunity_observations[0].opportunity_cause_id == expected_opportunity_id
    assert opportunity_observations[0].sale_opportunity_value_usd == 200_000
    pe_decision = next(
        decision for decision in result.policy_decisions(PrivateEquitySaleDecision) if decision.month_index == 12
    )
    assert pe_decision.decision_reason is PrivateEquitySaleDecisionReason.SALE_REQUESTED
    assert pe_decision.opportunity_id == expected_opportunity_id
    assert pe_decision.opportunity_cause_id == expected_opportunity_id
    assert pe_decision.requested_amount_usd == 100_000
    assert pe_decision.sale_opportunity_value_usd == 200_000
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PRIVATE_EQUITY,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        ),
        result.matrix("private_equity_sale_usd"),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PRIVATE_EQUITY, amount_field="cost_basis_usd"),
        result.matrix("private_equity_sale_basis_usd"),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PRIVATE_EQUITY, amount_field="tax_expense_usd"),
        result.matrix("private_equity_sale_tax_usd"),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        )
        - result.matrix("private_equity_sale_tax_usd"),
        result.matrix("private_equity_sale_usd") - result.matrix("private_equity_sale_tax_usd"),
    )

    actions = result.actions(SellPrivateEquityAction)
    assert len(actions) == 1
    assert actions[0].month_index == 12
    assert actions[0].event_id is None
    assert actions[0].event_type is None
    assert actions[0].opportunity_id == expected_opportunity_id
    assert actions[0].opportunity_cause_id == expected_opportunity_id
    assert actions[0].actor_id == "alpha"
    assert actions[0].policy_id == "private_equity_sale"
    assert actions[0].amount_usd == expected_sale
    assert actions[0].basis_usd == expected_basis
    assert actions[0].taxable_gain_usd == expected_taxable_gain
    assert_allclose(actions[0].estimated_tax_usd, expected_tax)
    assert_allclose(actions[0].after_tax_proceeds_usd, expected_after_tax_proceeds)
    assert actions[0].units_sold == 50
    assert actions[0].sold_fraction == 0.5
    assert actions[0].proceeds_destination is AccountType.CHECKING


def test_fixed_amount_private_equity_sale_rule_sells_on_market_opportunity() -> None:
    """A PE policy can sell a configured tranche when a market sale opportunity appears."""
    scenario = Scenario(
        scenario_id="automatic_private_equity_sale",
        label="Automatic Private Equity Sale",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=10_000
                ),
            ),
            assets=(
                PrivateEquityPosition(
                    asset_id="pe",
                    asset_type=AssetType.PRIVATE_EQUITY,
                    owner_actor_id="alpha",
                    value_usd=200_000,
                    cost_basis_usd=80_000,
                    units=100,
                ),
            ),
        ),
        policies=(
            PrivateEquitySalePolicy(
                policy_id="private_equity_sale",
                actor_id="alpha",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=50_000),
            ),
        ),
    )

    result = _run_scenario(
        scenario,
        horizon_months=6,
        market_provider=NoopMarketBundleProvider(private_equity_sale_opportunity_months=(6,)),
    )

    rollout = result.rollout(0)
    assert_allclose(rollout.series("private_equity_sale_usd")[6], 50_000)
    assert_allclose(rollout.series("private_equity_sale_basis_usd")[6], 20_000)
    expected_tax = 375.09
    assert_allclose(rollout.series("private_equity_sale_tax_usd")[6], expected_tax)
    assert_allclose(rollout.series("private_equity_value_usd")[6], 150_000)
    assert_allclose(rollout.series("cash_usd")[6], 60_000 - expected_tax)
    actions = result.actions(SellPrivateEquityAction)
    assert len(actions) == 1
    assert actions[0].event_id is None
    assert actions[0].event_type is None
    assert actions[0].amount_usd == 50_000
    assert_allclose(actions[0].after_tax_proceeds_usd, 50_000 - expected_tax)


def test_pydantic_rejects_private_equity_sale_policy_without_rule() -> None:
    """A private-equity sale policy must define an explicit opportunity participation rule."""
    with pytest.raises(ValidationError, match="sale_rule"):
        Scenario.model_validate(
            {
                "scenario_id": "missing_pe_sale_rule",
                "label": "Missing PE Sale Rule",
                "actors": [{"actor_id": "alpha", "label": "Alpha", "role": "primary_owner"}],
                "policies": [
                    {"policy_id": "private_equity_sale", "policy_type": "private_equity_sale", "actor_id": "alpha"}
                ],
            }
        )


if __name__ == "__main__":
    pytest_bazel.main()
