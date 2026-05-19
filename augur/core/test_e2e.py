"""E2e tests that exercise the augur simulation core in isolation.

These tests construct scenarios programmatically, run them through
the core engine with a deterministic market provider, and assert on financial
outcomes — no webapp, no FastAPI, no config files. Each test spells out the
expected computation so the test itself documents what the simulator should
produce.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_bazel
from numpy.testing import assert_allclose
from pydantic import ValidationError

from augur.core.accounting import ChartAccountRole, JournalEntryType, LotAssetClass, PostingSide
from augur.core.api import ScenarioRun, simulate_set
from augur.core.local_regulation import LocalRegulation, TaxRegime
from augur.core.market_bundle import MissingMarketFactorError, RequiredMarketKeys
from augur.core.market_bundle_test_support import NoopMarketBundleProvider
from augur.core.portfolio import load_portfolio_yaml
from augur.core.scenario_set import (
    AccountBalance,
    AccountType,
    Acquisition,
    Actor,
    ActorRole,
    AssetType,
    CheckingFloorSellPublicStockPolicy,
    CryptoAssetPosition,
    Financing,
    FinancingMode,
    FixedAmountPrivateEquitySaleRule,
    FundingDecisionType,
    GenericSp500StockPosition,
    InitialBalanceSheet,
    MarketPathObservation,
    MarketRequest,
    MonthlySpendDecision,
    MonthlySpendPolicy,
    ObligationStatus,
    ObligationType,
    OccupancyMode,
    OccupancyPlan,
    PartnerContributionDecision,
    PartnerEquityAccrualPolicy,
    PrivateEquityPosition,
    PrivateEquitySaleDecision,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySaleOpportunityObservation,
    PrivateEquitySalePolicy,
    PrivateEquitySaleRuleType,
    PropertyAssumptions,
    PropertyPurchaseEvent,
    PropertySaleBasisGainDetail,
    PropertySaleEvent,
    PropertySelection,
    PublicMarket,
    RentalMode,
    ReportMetric,
    ReportSpec,
    RolloutStatusType,
    Scenario,
    ScenarioSet,
    SellPrivateEquityEffect,
    SellPublicStockDecision,
    SellSp500Effect,
    SettlePropertySaleEffect,
    SpecialAssessmentEvent,
    TaxFilingStatus,
    TaxPaymentAllocationDetail,
    TaxPaymentTiming,
    TaxProfile,
    TransactionCosts,
    WholePropertyRentalPlan,
)

_TEST_LOCAL_REGULATION_BY_ID: dict[str, LocalRegulation] = {
    "san_francisco_ca": LocalRegulation(
        property_tax_regime=TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.SAN_FRANCISCO_SECURED_PROPERTY_TAX,
            TaxRegime.SAN_FRANCISCO_TRANSFER_TAX,
        ),
        property_tax_annual_pct=1.18,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="San Francisco secured property-tax default used by the consolidated house model.",
    ),
    "vallejo_ca": LocalRegulation(
        property_tax_regime=TaxRegime.VALLEJO_PROPERTY_TAX,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.VALLEJO_PROPERTY_TAX,
        ),
        property_tax_annual_pct=1.1,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="Vallejo mainland property-tax default around 1.1%.",
    ),
    "mare_island_vallejo_ca": LocalRegulation(
        property_tax_regime=TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS,
        default_tax_regimes=(
            TaxRegime.CALIFORNIA_PROP13,
            TaxRegime.CALIFORNIA_TRANSFER_TAX,
            TaxRegime.FEDERAL_MORTGAGE_INTEREST,
            TaxRegime.FEDERAL_CAPITAL_GAINS,
            TaxRegime.CALIFORNIA_INCOME_TAX,
            TaxRegime.MARE_ISLAND_SPECIAL_ASSESSMENTS,
        ),
        property_tax_annual_pct=2.4,
        local_transfer_tax_pct=0,
        special_assessment_annual_usd=0,
        notes="Mare Island default includes high local special assessments at roughly 2.4%.",
    ),
}


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
    run = simulate_set(
        scenario_set,
        market_provider=market_provider or NoopMarketBundleProvider(),
        local_regulation_by_id=_TEST_LOCAL_REGULATION_BY_ID,
    )
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
    matrix = np.zeros_like(result.matrix(ReportMetric.CASH_USD), dtype="float64")
    journal_type_by_id = {entry.journal_entry_id: entry.journal_entry_type for entry in result.journal_entries()}
    for posting in result.postings(role=role, side=side):
        if journal_entry_type is not None and journal_type_by_id[posting.journal_entry_id] is not journal_entry_type:
            continue
        matrix[posting.rollout_index, posting.month_index] += posting.amount_usd
    return matrix


def _balance_snapshot_matrix(result: ScenarioRun, *, role: ChartAccountRole) -> np.ndarray:
    matrix = np.zeros_like(result.matrix(ReportMetric.CASH_USD), dtype="float64")
    for snapshot in result.balance_snapshots(role=role):
        matrix[snapshot.rollout_index, snapshot.month_index] += snapshot.balance_usd
    return matrix


def _lot_disposition_matrix(result: ScenarioRun, *, asset_class: LotAssetClass, amount_field: str) -> np.ndarray:
    matrix = np.zeros_like(result.matrix(ReportMetric.CASH_USD), dtype="float64")
    for disposition in result.lot_dispositions(asset_class=asset_class):
        matrix[disposition.rollout_index, disposition.month_index] += getattr(disposition, amount_field)
    return matrix


def _accounting_detail_matrix(result: ScenarioRun, detail_type: type[Any], amount_field: str) -> np.ndarray:
    matrix = np.zeros_like(result.matrix(ReportMetric.CASH_USD), dtype="float64")
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


def _mortgage_scenario(scenario_id: str) -> Scenario:
    """Property + 30Y fixed mortgage scenario that produces obligations,
    funding_decisions, and settlement_results in the in-memory arrays — the
    debug streams gated by the new include_* flags."""
    return Scenario(
        scenario_id=scenario_id,
        label=scenario_id.replace("_", " ").title(),
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=2.5, closing_cost_sell_pct=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=200_000,
                ),
            )
        ),
    )


def test_report_spec_event_streams_default_to_empty_on_the_wire() -> None:
    """The four per-rollout-per-month event streams (obligations, funding
    decisions, settlement results, failure events) dominate response size
    and aren't consumed by any current frontend, so the default `ReportSpec`
    drops them from the wire. The in-memory `ScenarioRunArrays` still
    carries them for backend tests and would-be debug consumers."""
    scenario_set = ScenarioSet(
        scenario_set_id="gates_default",
        title="Gates Default",
        market_request=MarketRequest(market_model_id="e2e_noop", rollout_count=1, horizon_months=2, seed=0),
        scenarios=(_mortgage_scenario("gates_default"),),
    )

    run = simulate_set(
        scenario_set, market_provider=NoopMarketBundleProvider(), local_regulation_by_id=_TEST_LOCAL_REGULATION_BY_ID
    )
    result = run.scenario("gates_default")
    assert result.arrays is not None
    assert len(result.arrays.obligations) > 0
    assert len(result.arrays.funding_decisions) > 0
    assert len(result.arrays.settlement_results) > 0

    response_result = run.to_response().scenario_results[0]
    assert response_result.obligations == ()
    assert response_result.funding_decisions == ()
    assert response_result.settlement_results == ()
    assert response_result.failure_events == ()


def test_report_spec_event_streams_opt_in_round_trips_array_data() -> None:
    """When the gates are flipped on, each event stream round-trips to the
    response in the same shape as the in-memory arrays — same count, same
    field values per row."""
    scenario_set = ScenarioSet(
        scenario_set_id="gates_opt_in",
        title="Gates Opt In",
        market_request=MarketRequest(market_model_id="e2e_noop", rollout_count=1, horizon_months=2, seed=0),
        report_spec=ReportSpec(
            include_obligations=True,
            include_funding_decisions=True,
            include_settlement_results=True,
            include_failure_events=True,
        ),
        scenarios=(_mortgage_scenario("gates_opt_in"),),
    )

    run = simulate_set(
        scenario_set, market_provider=NoopMarketBundleProvider(), local_regulation_by_id=_TEST_LOCAL_REGULATION_BY_ID
    )
    result = run.scenario("gates_opt_in")
    response_result = run.to_response().scenario_results[0]

    assert result.arrays is not None
    assert len(response_result.obligations) == len(result.arrays.obligations)
    assert len(response_result.funding_decisions) == len(result.arrays.funding_decisions)
    assert len(response_result.settlement_results) == len(result.arrays.settlement_results)
    assert len(response_result.failure_events) == len(result.arrays.failure_events)


def test_cash_only_no_activity_preserves_balance() -> None:
    """Agent holds $100k in checking, no property, no investments, no spending.
    Flat market. Cash should remain exactly $100k at every month."""
    scenario = _cash_only_scenario(cash_usd=100_000)
    result = _run_scenario(scenario, horizon_months=12)

    # Single rollout, 13 months (0..12)
    assert result.matrix(ReportMetric.CASH_USD).shape == (1, 13)
    assert_allclose(result.series(ReportMetric.CASH_USD), 100_000)
    assert_allclose(result.matrix(ReportMetric.GENERIC_SP500_VALUE_USD), 0)
    assert_allclose(result.matrix(ReportMetric.PRIVATE_EQUITY_VALUE_USD), 0)
    assert_allclose(result.matrix(ReportMetric.PROPERTY_VALUE_USD), 0)
    assert_allclose(result.series(ReportMetric.NET_WORTH_USD), 100_000)
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
            property_id="test_property", location_id="vallejo_ca", purchase_price_usd=100_000
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

    assert result.matrix(ReportMetric.CASH_USD).shape == (1, 4)
    assert_allclose(result.series(ReportMetric.CASH_USD), 0)
    # SP500 value tracks multiplier: 50k * [1.0, 1.1, 1.2, 1.3]
    assert_allclose(result.series(ReportMetric.GENERIC_SP500_VALUE_USD), [50_000, 55_000, 60_000, 65_000])
    # Net worth = SP500 only
    assert_allclose(result.series(ReportMetric.NET_WORTH_USD), [50_000, 55_000, 60_000, 65_000])


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

    assert_allclose(result.series(ReportMetric.CASH_USD), 30_000)
    assert_allclose(result.series(ReportMetric.GENERIC_SP500_VALUE_USD), 70_000)
    assert_allclose(result.series(ReportMetric.NET_WORTH_USD), 100_000)


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
    assert_allclose(result.series(ReportMetric.CASH_USD)[0], 100_000)
    # Month 1: 100k - 5k = 95k
    assert_allclose(result.series(ReportMetric.CASH_USD)[1], 95_000)
    # Month 6: 100k - 6*5k = 70k
    assert_allclose(result.series(ReportMetric.CASH_USD)[6], 70_000)
    # Month 12: 100k - 12*5k = 40k
    assert_allclose(result.terminal(ReportMetric.CASH_USD), 40_000)
    # Verify spend array
    assert_allclose(result.series(ReportMetric.MONTHLY_SPEND_USD)[0], 0)
    assert_allclose(result.series(ReportMetric.MONTHLY_SPEND_USD)[1], 5_000)
    assert_allclose(result.series(ReportMetric.MONTHLY_SPEND_USD)[12], 5_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MONTHLY_LIVING_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.MONTHLY_SPEND_USD),
    )
    # The actor decision trace records monthly-spend decisions; the underlying
    # cash debit is the canonical detail surface (asserted above).
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

    assert_allclose(result.matrix(ReportMetric.CASH_USD)[:, 0], 100_000)
    assert_allclose(result.matrix(ReportMetric.CASH_USD)[:, 1], 95_000)
    assert_allclose(result.matrix(ReportMetric.CASH_USD)[:, 2], 90_000)
    assert_allclose(result.matrix(ReportMetric.MONTHLY_SPEND_USD)[:, 1:], 5_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MONTHLY_LIVING_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.MONTHLY_SPEND_USD),
    )
    # Per-rollout/per-month attribution is recorded in the policy-decision trace
    # (one decision row per rollout/month where the policy fired); the ledger
    # postings cover the cash debit itself.
    assert [
        (decision.rollout_index, decision.month_index, decision.amount_usd)
        for decision in result.policy_decisions(MonthlySpendDecision)
    ] == [(0, 1, 5_000), (1, 1, 5_000), (0, 2, 5_000), (1, 2, 5_000)]


def test_fixed_rate_mortgage_amortizes_and_purchase_cash_outlay_posts_at_month_zero() -> None:
    """Agent buys a $500k property with 20% down and 30-year fixed financing at 6%.
    With a $200k starting checking balance the down payment, closing costs, and
    twelve months of scheduled mortgage payments all settle from cash; every
    mortgage payment flows through the obligation pipeline (`MORTGAGE_PAYMENT`
    obligation, cash funding decision, paid settlement)."""
    scenario = Scenario(
        scenario_id="mortgage_amortization",
        label="Mortgage Amortization",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=2.5, closing_cost_sell_pct=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=200_000,
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
    # Cash at month 0: $200k - $100k down payment - $12.5k buy closing = $87.5k.
    # No mortgage subtraction in month 0 because the first mortgage obligation is
    # raised against month 1's scheduled payment.
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[0], 87_500)
    status = rollout.status()
    assert status.status == RolloutStatusType.ACTIVE
    assert status.failed_obligation_count == 0
    assert status.unpaid_obligation_usd == 0
    assert_allclose(rollout.series(ReportMetric.PROPERTY_VALUE_USD)[0], 500_000)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[0], loan_amount)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_INTEREST_USD)[1], expected_month_1_interest)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_PRINCIPAL_USD)[1], expected_month_1_principal)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_PAYMENT_USD)[1], payment)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[1], loan_amount - expected_month_1_principal)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.MORTGAGE_INTEREST_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.MORTGAGE_PAYABLE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
        ),
        result.matrix(ReportMetric.MORTGAGE_PRINCIPAL_USD),
    )
    # Mortgage payment detail is exposed via obligation + settlement rows and the
    # MORTGAGE_INTEREST/MORTGAGE_PRINCIPAL ledger postings asserted above; the
    # standalone PayMortgageEffect row has been collapsed away.
    assert result.arrays is not None
    mortgage_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.MORTGAGE_PAYMENT
    )
    assert len(mortgage_obligations) == 12
    assert {obligation.creditor_id for obligation in mortgage_obligations} == {"mortgage_lender"}
    assert {obligation.status for obligation in mortgage_obligations} == {ObligationStatus.PAID}
    assert_allclose(sum(obligation.amount_due_usd for obligation in mortgage_obligations), payment * 12)
    cash_fund_decisions = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith(ObligationType.MORTGAGE_PAYMENT.value)
        and decision.decision_type is FundingDecisionType.USE_CASH
    )
    assert len(cash_fund_decisions) == 12
    assert all(decision.funded_cash_usd > 0 for decision in cash_fund_decisions)


def test_mortgage_shortfall_records_failed_obligation_and_failure_event() -> None:
    """When cash cannot fund the scheduled mortgage payment and no policy can
    cover the shortfall, the obligation reports UNPAID, the settlement records
    UNPAID, and the rollout transitions to FAILED with one failure event per
    missed payment month."""
    scenario = Scenario(
        scenario_id="mortgage_shortfall",
        label="Mortgage Shortfall",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
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
    horizon_months = 3
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    mortgage_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.MORTGAGE_PAYMENT
    )
    assert len(mortgage_obligations) == horizon_months
    # Cash starts at $0 ($100k - $100k down payment); no buffer for mortgage.
    assert {obligation.status for obligation in mortgage_obligations} == {ObligationStatus.UNPAID}
    assert all(obligation.amount_paid_usd == 0 for obligation in mortgage_obligations)
    mortgage_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("mortgage_payment")
    )
    assert len(mortgage_failures) == horizon_months
    assert all(event.unpaid_amount_usd > 0 for event in mortgage_failures)
    status = result.rollout(0).status()
    assert status.status == RolloutStatusType.FAILED
    # The actor also misses the monthly property tax obligation (SF has a
    # non-zero property tax) since cash starts at $0. The count is one mortgage
    # failure + one property-tax failure per month.
    property_tax_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("property_tax")
    )
    assert len(property_tax_failures) == horizon_months
    assert status.failed_obligation_count == horizon_months * 2
    assert status.first_failed_obligation_month_index == 1
    assert status.unpaid_obligation_usd > 0
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("mortgage_payment")
        and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) == horizon_months
    # No public stock and no sale policy: shortfall reflects the full payment due.
    assert all(decision.shortfall_usd > 0 for decision in unfunded)


def test_mortgage_shortfall_can_be_rescued_by_checking_floor_sale_policy() -> None:
    """A CheckingFloorSellPublicStockPolicy in the actor's program rescues a
    mortgage shortfall by selling SP500 stock when the projected cash after the
    mortgage payment would fall below the floor. The proactive (per-month)
    branch of the policy can't reach the mortgage shortfall because it runs
    before the settlement; the obligation-funding branch sees the missing
    cash and emits a SELL_PUBLIC_STOCK funding decision tagged with the
    mortgage obligation."""
    scenario = Scenario(
        scenario_id="mortgage_rescue",
        label="Mortgage Rescue",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="mortgage_funding_sale", actor_id="alpha", floor_usd=0, sale_amount_usd=2_500
            ),
        ),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500", owner_actor_id="alpha", value_usd=200_000, cost_basis_usd=200_000
                ),
            ),
        ),
    )
    horizon_months = 3
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    mortgage_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.MORTGAGE_PAYMENT
    )
    assert len(mortgage_obligations) == horizon_months
    assert {obligation.status for obligation in mortgage_obligations} == {ObligationStatus.PAID}
    assert tuple(event for event in result.arrays.failure_events) == ()
    status = result.rollout(0).status()
    assert status.status == RolloutStatusType.ACTIVE
    sale_funding_decisions = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("mortgage_payment")
        and decision.decision_type is FundingDecisionType.SELL_PUBLIC_STOCK
    )
    assert len(sale_funding_decisions) == horizon_months
    assert {decision.policy_id for decision in sale_funding_decisions} == {"mortgage_funding_sale"}
    assert all(decision.funded_cash_usd > 0 for decision in sale_funding_decisions)


def test_mortgage_obligation_continues_projection_after_failure() -> None:
    """A FAILED rollout still keeps its projection arrays (mortgage balance keeps
    amortizing on schedule even when monthly payments go unpaid). Failure events
    track every unpaid month; downstream consumers can decide whether to treat
    the trajectory as terminated for reporting purposes."""
    scenario = Scenario(
        scenario_id="mortgage_failure_projection",
        label="Mortgage Failure Projection",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=20, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
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
    horizon_months = 6
    result = _run_scenario(scenario, horizon_months=horizon_months)
    rollout = result.rollout(0)
    loan_amount = 400_000
    monthly_rate = 0.06 / 12
    scheduled_payment = loan_amount * monthly_rate * (1 + monthly_rate) ** 360 / ((1 + monthly_rate) ** 360 - 1)
    expected_month_1_principal = scheduled_payment - loan_amount * monthly_rate
    # Mortgage balance still drops by scheduled principal each month, even when
    # payments fail — the projection is the scenario schedule, not the per-rollout
    # accounting trace.
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[1], loan_amount - expected_month_1_principal)
    assert rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[6] < loan_amount
    assert result.arrays is not None
    mortgage_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("mortgage_payment")
    )
    # One failure per scheduled mortgage month (1..6).
    assert len(mortgage_failures) == horizon_months
    assert {event.month_index for event in mortgage_failures} == set(range(1, horizon_months + 1))


def test_special_assessment_event_settles_as_obligation_when_cash_available() -> None:
    """A `SpecialAssessmentEvent` produces a `SPECIAL_ASSESSMENT` obligation due in
    the event month. When cash covers the amount, the obligation settles PAID and
    the rollout stays ACTIVE; cash drops by the assessment amount in that month."""
    scenario = Scenario(
        scenario_id="special_assessment_happy",
        label="Special Assessment Happy",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=50_000
                ),
            )
        ),
        events=(SpecialAssessmentEvent(event_id="hoa_assessment", month_index=3, amount_usd=10_000),),
    )
    result = _run_scenario(scenario, horizon_months=6)
    assert result.arrays is not None
    special_assessment_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.SPECIAL_ASSESSMENT
    )
    assert len(special_assessment_obligations) == 1
    assessment = special_assessment_obligations[0]
    assert assessment.month_index == 3
    assert assessment.amount_due_usd == 10_000
    assert assessment.status is ObligationStatus.PAID
    assert tuple(event for event in result.arrays.failure_events) == ()
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    cash_series = result.rollout(0).series(ReportMetric.CASH_USD)
    assert cash_series[2] == 50_000
    assert cash_series[3] == 40_000


def test_special_assessment_event_fails_rollout_when_unfundable() -> None:
    """A required `SPECIAL_ASSESSMENT` obligation that can't be funded fails the
    rollout: emits a `FailureEvent`, flips status to FAILED, and records an
    UNFUNDED funding decision."""
    scenario = Scenario(
        scenario_id="special_assessment_unfundable",
        label="Special Assessment Unfundable",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=500
                ),
            )
        ),
        events=(SpecialAssessmentEvent(event_id="hoa_assessment", month_index=2, amount_usd=25_000),),
    )
    result = _run_scenario(scenario, horizon_months=6)
    assert result.arrays is not None
    special_assessment_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.SPECIAL_ASSESSMENT
    )
    assert len(special_assessment_obligations) == 1
    assert special_assessment_obligations[0].status is ObligationStatus.PARTIALLY_PAID
    failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("special_assessment")
    )
    assert len(failures) == 1
    assert failures[0].unpaid_amount_usd > 0
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("special_assessment")
        and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) == 1
    assert unfunded[0].shortfall_usd > 0


def _property_obligation_scenario(
    *,
    scenario_id: str,
    initial_cash_usd: float,
    insurance_annual_usd: float = 0,
    maintenance_pct: float = 0,
    hoa_monthly_usd: float = 0,
    location_id: str = "san_francisco_ca",
    purchase_price_usd: float = 500_000,
    events: tuple = (),
) -> Scenario:
    """A minimal property scenario that exercises the property carrying-cost
    obligation pipeline. Per-line costs (property tax, HOA, insurance,
    maintenance) settle through `_settle_required_cash_obligations` and may
    fail the rollout if cash and funding policies cannot cover them.

    `hoa_monthly_usd` is conveyed via a `PropertyPurchaseEvent` (the canonical
    HOA-monthly knob); 0 means HOA stays at the location default.
    """
    purchase_events: tuple = ()
    if hoa_monthly_usd > 0:
        purchase_events = (
            PropertyPurchaseEvent(
                event_id="purchase", month_index=0, property_id="test_property", hoa_monthly_usd=hoa_monthly_usd
            ),
        )
    return Scenario(
        scenario_id=scenario_id,
        label=scenario_id.replace("_", " ").title(),
        actors=(_simple_actor(),),
        events=purchase_events + events,
        property_selection=PropertySelection(
            property_id="test_property", location_id=location_id, purchase_price_usd=purchase_price_usd
        ),
        # Cash financing: no down-payment buy-out, no mortgage obligation.
        # This isolates the test to the carrying-cost obligation we want to
        # exercise rather than mixing in mortgage failures.
        financing=Financing(financing_mode=FinancingMode.CASH),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(
            insurance_annual_usd=insurance_annual_usd, maintenance_pct=maintenance_pct
        ),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=initial_cash_usd,
                ),
            )
        ),
    )


def test_property_tax_obligation_settles_when_cash_available() -> None:
    """Happy path: the four property carrying-cost lines (property tax, HOA,
    insurance, maintenance) now settle as PAID obligations on the trace. The
    cash trajectory matches the pre-refactor behavior because the in-loop
    settlement deducts each cost from current_cash at month start (mirroring
    the prior net_property_cash_flow math)."""
    scenario = _property_obligation_scenario(
        scenario_id="property_carrying_costs_paid",
        initial_cash_usd=600_000,  # enough for $500k cash purchase + carrying costs
        insurance_annual_usd=1_200,
        maintenance_pct=1,
        hoa_monthly_usd=300,
    )
    result = _run_scenario(scenario, horizon_months=3)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    # Each of the four cost lines emits one obligation per month-1..horizon
    # (month 0 zeros out via the engine's first-month carry-cost zeroing).
    obligation_types_seen = {
        obligation.obligation_type
        for obligation in result.arrays.obligations
        if obligation.obligation_type
        in {
            ObligationType.PROPERTY_TAX,
            ObligationType.HOA_DUES,
            ObligationType.INSURANCE_PREMIUM,
            ObligationType.MAINTENANCE,
        }
    }
    assert obligation_types_seen == {
        ObligationType.PROPERTY_TAX,
        ObligationType.HOA_DUES,
        ObligationType.INSURANCE_PREMIUM,
        ObligationType.MAINTENANCE,
    }
    # All carrying-cost obligations PAY when cash is sufficient.
    for obligation_type in obligation_types_seen:
        obligations = tuple(
            obligation for obligation in result.arrays.obligations if obligation.obligation_type is obligation_type
        )
        assert {obligation.status for obligation in obligations} == {ObligationStatus.PAID}
    # No failure events fired.
    assert tuple(event for event in result.arrays.failure_events) == ()
    # The ledger postings on the expense roles continue to reflect the actual
    # cost (now driven by the obligation settlement JE rather than the operating
    # cash-flow JE). This is the reconciliation contract the existing
    # `test_every_monthly_flow_metric_reconciles_to_canonical_detail_surface`
    # guard depends on.
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PROPERTY_TAX_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.PROPERTY_TAX_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.HOA_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.HOA_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.INSURANCE_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.INSURANCE_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.MAINTENANCE_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.MAINTENANCE_USD),
    )


def test_property_tax_obligation_fails_rollout_when_unfundable() -> None:
    """Cash-strapped property: monthly property tax can't be paid → PROPERTY_TAX
    obligation emits UNPAID, FailureEvent fires, rollout flips to FAILED."""
    scenario = _property_obligation_scenario(
        scenario_id="property_tax_unfundable",
        initial_cash_usd=10,  # Far below the monthly property-tax obligation.
        insurance_annual_usd=0,
        maintenance_pct=0,
    )
    result = _run_scenario(scenario, horizon_months=3)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    property_tax_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.PROPERTY_TAX
    )
    # 3 months of unfunded property tax obligations.
    assert len(property_tax_obligations) == 3
    assert {obligation.status for obligation in property_tax_obligations} == {ObligationStatus.UNPAID}
    failures = tuple(event for event in result.arrays.failure_events if event.obligation_id.startswith("property_tax"))
    assert len(failures) == 3
    assert all(event.unpaid_amount_usd > 0 for event in failures)
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("property_tax") and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) == 3
    assert all(decision.shortfall_usd > 0 for decision in unfunded)


def test_hoa_dues_obligation_fails_rollout_when_unfundable() -> None:
    """Cash-strapped property with explicit HOA dues: cash starts at a value
    that covers property tax for the first month but not the HOA dues. The
    HOA_DUES obligation flips the rollout to FAILED."""
    # SF property tax on $100k purchase is small (≈ $100/mo). Hoa monthly $5000
    # blows past available cash starting in month 1.
    scenario = _property_obligation_scenario(
        scenario_id="hoa_dues_unfundable",
        initial_cash_usd=500,
        insurance_annual_usd=0,
        maintenance_pct=0,
        hoa_monthly_usd=5_000,
        purchase_price_usd=100_000,
    )
    result = _run_scenario(scenario, horizon_months=2)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    hoa_failures = tuple(event for event in result.arrays.failure_events if event.obligation_id.startswith("hoa_dues"))
    assert len(hoa_failures) >= 1
    assert all(event.unpaid_amount_usd > 0 for event in hoa_failures)
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("hoa_dues") and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) >= 1


def test_insurance_premium_obligation_fails_rollout_when_unfundable() -> None:
    """Cash-strapped property with very high insurance: the INSURANCE_PREMIUM
    obligation flips the rollout to FAILED."""
    scenario = _property_obligation_scenario(
        scenario_id="insurance_premium_unfundable",
        initial_cash_usd=100,
        insurance_annual_usd=120_000,  # $10k/month — far above starting cash.
        maintenance_pct=0,
        purchase_price_usd=100_000,
    )
    result = _run_scenario(scenario, horizon_months=2)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    insurance_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("insurance_premium")
    )
    assert len(insurance_failures) >= 1
    assert all(event.unpaid_amount_usd > 0 for event in insurance_failures)


def test_maintenance_obligation_fails_rollout_when_unfundable() -> None:
    """Cash-strapped property with very high maintenance pct: the MAINTENANCE
    obligation flips the rollout to FAILED."""
    # Maintenance = property_value * maintenance_pct/100 / 12 per month.
    # 500k * 50% / 12 ≈ $20,833/month — far above starting cash.
    scenario = _property_obligation_scenario(
        scenario_id="maintenance_unfundable",
        initial_cash_usd=100,
        insurance_annual_usd=0,
        maintenance_pct=50,
        purchase_price_usd=500_000,
    )
    result = _run_scenario(scenario, horizon_months=2)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    maintenance_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("maintenance")
    )
    assert len(maintenance_failures) >= 1
    assert all(event.unpaid_amount_usd > 0 for event in maintenance_failures)


def test_partner_contribution_obligation_fails_rollout_when_partner_cannot_fund() -> None:
    """The contributing partner has an explicitly modeled checking account with
    insufficient cash to cover the configured contribution. The
    PARTNER_CONTRIBUTION obligation flips the rollout to FAILED.

    When a partner has *any* configured CHECKING account, the obligation
    settles strictly against that balance — running out fails the rollout.
    Partners with no configured account default to off-trace funding (legacy
    behavior preserved for existing tests).
    """
    horizon_months = 6
    scenario = Scenario(
        scenario_id="partner_contribution_unfundable",
        label="Partner Contribution Unfundable",
        actors=(_simple_actor(), Actor(actor_id="beta", label="Beta", role=ActorRole.EQUITY_BUILDING_OCCUPANT)),
        property_selection=PropertySelection(
            property_id="test_property", location_id="vallejo_ca", purchase_price_usd=100_000
        ),
        financing=Financing(financing_mode=FinancingMode.CASH),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=0, closing_cost_sell_pct=0),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=0, maintenance_pct=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=200_000,
                ),
                # Partner has only $1500 — covers month 1 ($1000) but month 2's
                # contribution ($1000) exhausts the balance ($500 left). Month 3
                # onward have a hard shortfall.
                AccountBalance(
                    account_id="partner_checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="beta",
                    balance_usd=1_500,
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

    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    partner_contribution_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.PARTNER_CONTRIBUTION
    )
    # One obligation per occupied month — month 1..6 (six in this scenario).
    assert len(partner_contribution_obligations) == horizon_months
    assert {obligation.actor_id for obligation in partner_contribution_obligations} == {"beta"}
    assert {obligation.creditor_id for obligation in partner_contribution_obligations} == {"alpha"}
    partner_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("partner_contribution")
    )
    assert len(partner_failures) >= 1
    assert all(event.unpaid_amount_usd > 0 for event in partner_failures)
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("partner_contribution")
        and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) >= 1
    assert all(decision.shortfall_usd > 0 for decision in unfunded)


def _estimated_tax_obligations(result: ScenarioRun) -> tuple:
    """Filter result.arrays.obligations down to quarterly estimated-tax rows."""
    assert result.arrays is not None
    return tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.ESTIMATED_TAX_PAYMENT
    )


def _annual_tax_obligations(result: ScenarioRun) -> tuple:
    """Filter result.arrays.obligations down to year-end annual-tax rows."""
    assert result.arrays is not None
    return tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.ANNUAL_TAX_PAYMENT
    )


def test_quarterly_estimated_tax_happy_path_q1_through_q4_with_zero_year_end() -> None:
    """A scenario with a $50k SP500 sale in month 2 generates Q1/Q2/Q3/Q4
    estimated payment obligations on the IRS calendar (Apr 15, Jun 15, Sep 15,
    Jan 15 of year 2). With `prior_year_tax_usd` tuned so Q1+Q2+Q3 covers the
    full year tax, the year-end true-up at month 11 (Dec 31) accrues zero —
    cash dips at months 3, 5, 8, 12 (Apr, Jun, Sep, Jan-of-year-2) but NOT at
    month 11.
    """
    horizon_months = 13  # Through Jan 15 of year 1 (month 12) plus padding.
    # First, run the scenario with a placeholder prior_year_tax_usd to compute
    # the actual sale tax — this lets the test pin a year-end value without
    # hand-computing bracket-aware math here.
    base_scenario = Scenario(
        scenario_id="estimated_tax_happy_probe",
        label="Estimated Tax Happy Probe",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(prior_year_tax_usd=0),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=0,
                ),
            ),
        ),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=200_000, sale_amount_usd=50_000
            ),
        ),
    )
    probe = _run_scenario(base_scenario, horizon_months=horizon_months)
    total_year_tax_usd = float(np.sum(probe.rollout(0).series(ReportMetric.TOTAL_INCOME_TAX_USD)))
    assert total_year_tax_usd > 0, "probe scenario must produce some tax for the test to be meaningful"
    # Setting prior_year_tax_usd to (4/3) of the actual year tax sizes each
    # quarter at total/3; by month 11 (Dec 31), three quarters have paid the
    # full year bill, so the year-end true-up clamps to zero.
    prior_year_tax_usd = total_year_tax_usd * 4.0 / 3.0

    scenario = Scenario(
        scenario_id="estimated_tax_happy",
        label="Estimated Tax Happy",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(prior_year_tax_usd=prior_year_tax_usd),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=100_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    cost_basis_usd=0,
                ),
            ),
        ),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=200_000, sale_amount_usd=50_000
            ),
        ),
    )
    result = _run_scenario(scenario, horizon_months=horizon_months)

    estimated = _estimated_tax_obligations(result)
    # Exactly four quarterly obligations for tax year 0 (Q4 of year 0 lands at
    # month 12 = Jan 15 of year 1).
    assert {obligation.month_index for obligation in estimated} == {3, 5, 8, 12}
    assert all(obligation.status is ObligationStatus.PAID for obligation in estimated)
    expected_per_quarter = prior_year_tax_usd / 4.0
    for obligation in estimated:
        assert_allclose(obligation.amount_due_usd, expected_per_quarter)

    # Year-end (Dec 31 of year 0 = month 11) accrues zero because Q1+Q2+Q3
    # already cover the year tax. The function records no zero-amount
    # obligation so the year-end obligation set is empty.
    annual = _annual_tax_obligations(result)
    assert annual == ()

    rollout = result.rollout(0)
    cash = rollout.series(ReportMetric.CASH_USD)
    # Cash starts at $100k + $50k sale at month 2 (sale is recorded into cash
    # via the SP500 sale because the checking-floor policy fires when the
    # projected post-sale cash would otherwise drop below $200k — here cash is
    # well below the floor every month, but the sale is one-shot at month 2
    # because the SP500 pool is fully liquidated after one $50k sale).
    # After month 2, cash is $150k. Then dips at months 3, 5, 8, 12 by Q/4.
    assert cash[2] == pytest.approx(150_000.0)
    # Cash should be unchanged between non-payment months. Month 10 → 11 → 12
    # exercise the "no dip at Dec 31" requirement.
    assert cash[11] == pytest.approx(cash[10])
    # Cash drops by exactly `expected_per_quarter` at each of months 3, 5, 8, 12.
    for due_month in (3, 5, 8, 12):
        assert cash[due_month] == pytest.approx(cash[due_month - 1] - expected_per_quarter)


def test_quarterly_estimated_tax_first_year_with_no_prior_tax_skips_quarterlies() -> None:
    """When no prior-year tax is supplied, no quarterly estimated-tax
    obligations emit for year 0 — the year-end true-up settles the full
    actual year tax. (Phase 4b behavior: inline TaxActor can't reach
    forward in time to size year-0 quarterlies as 90% of current year's
    actual tax. Users wanting year-0 quarterlies supply
    `tax_profile.prior_year_tax_usd` as a user-settable knob; see
    `test_quarterly_estimated_tax_multi_year_uses_prior_year_tax_with_high_agi_threshold`.)
    """
    scenario = Scenario(
        scenario_id="estimated_tax_first_year",
        label="Estimated Tax First Year",
        actors=(_simple_actor(),),
        # No `prior_year_tax_usd` — no year-0 quarterlies will fire.
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=200_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=100_000,
                    cost_basis_usd=0,
                ),
            ),
        ),
        policies=(
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=400_000, sale_amount_usd=100_000
            ),
        ),
    )
    result = _run_scenario(scenario, horizon_months=13)
    total_year_tax = float(np.sum(result.rollout(0).series(ReportMetric.TOTAL_INCOME_TAX_USD)))
    estimated = _estimated_tax_obligations(result)
    assert estimated == ()
    # Year-end picks up the full year tax.
    annual = _annual_tax_obligations(result)
    assert len(annual) == 1
    assert_allclose(annual[0].amount_due_usd, total_year_tax)


def test_quarterly_estimated_tax_multi_year_uses_prior_year_tax_with_high_agi_threshold() -> None:
    """A scenario with `prior_year_tax_usd=40_000` produces four equal $10k
    quarterly obligations (100% prior-year safe-harbor, divided by 4) at the
    standard IRS calendar months. A high-AGI variant (annual income above
    $150k single) uses the 110% safe-harbor — four $11k payments — and a
    correspondingly smaller year-end true-up.
    """
    horizon_months = 13

    def _scenario_with_income(scenario_id: str, *, annual_ordinary_income_usd: float) -> Scenario:
        return Scenario(
            scenario_id=scenario_id,
            label=scenario_id.replace("_", " ").title(),
            actors=(_simple_actor(),),
            tax_profile=TaxProfile(annual_ordinary_income_usd=annual_ordinary_income_usd, prior_year_tax_usd=40_000),
            initial_balance_sheet=InitialBalanceSheet(
                accounts=(
                    AccountBalance(
                        account_id="checking",
                        account_type=AccountType.CHECKING,
                        owner_actor_id="alpha",
                        balance_usd=1_000_000,
                    ),
                ),
                assets=(
                    GenericSp500StockPosition(
                        asset_id="sp500",
                        asset_type=AssetType.GENERIC_SP500_STOCK,
                        owner_actor_id="alpha",
                        value_usd=200_000,
                        cost_basis_usd=0,
                    ),
                ),
            ),
            policies=(
                CheckingFloorSellPublicStockPolicy(
                    policy_id="checking_floor", actor_id="alpha", floor_usd=2_000_000, sale_amount_usd=200_000
                ),
            ),
        )

    # Low-AGI: $100k ordinary income is below the $150k single high-AGI
    # threshold; 100% prior-year safe-harbor → each quarter = $10k.
    low_agi_result = _run_scenario(
        _scenario_with_income("estimated_tax_multi_year_low_agi", annual_ordinary_income_usd=100_000),
        horizon_months=horizon_months,
    )
    low_estimated = _estimated_tax_obligations(low_agi_result)
    assert {obligation.month_index for obligation in low_estimated} == {3, 5, 8, 12}
    for obligation in low_estimated:
        assert_allclose(obligation.amount_due_usd, 10_000)

    # High-AGI: $200k ordinary income exceeds the $150k single threshold;
    # 110% prior-year safe-harbor → each quarter = $11k.
    high_agi_result = _run_scenario(
        _scenario_with_income("estimated_tax_multi_year_high_agi", annual_ordinary_income_usd=200_000),
        horizon_months=horizon_months,
    )
    high_estimated = _estimated_tax_obligations(high_agi_result)
    assert {obligation.month_index for obligation in high_estimated} == {3, 5, 8, 12}
    for obligation in high_estimated:
        assert_allclose(obligation.amount_due_usd, 11_000)

    # Year-end true-up = `max(0, actual_tax - sum_of_all_four_quarterly_paid)`.
    # The Dec 31 year-end "looks ahead" to credit the scheduled Q4 payment
    # (which lands on Jan 15 of year N+1) because the simulator processes
    # the whole horizon's estimated payments before sizing the year-end
    # residual — so the residual is the gap between actual tax and total
    # estimated paid, not just Q1+Q2+Q3.
    low_annual = _annual_tax_obligations(low_agi_result)
    high_annual = _annual_tax_obligations(high_agi_result)
    low_total_tax = float(np.sum(low_agi_result.rollout(0).series(ReportMetric.TOTAL_INCOME_TAX_USD)))
    high_total_tax = float(np.sum(high_agi_result.rollout(0).series(ReportMetric.TOTAL_INCOME_TAX_USD)))
    low_year_end = low_annual[0].amount_due_usd if low_annual else 0.0
    high_year_end = high_annual[0].amount_due_usd if high_annual else 0.0
    assert_allclose(low_year_end, max(0.0, low_total_tax - 4 * 10_000))
    assert_allclose(high_year_end, max(0.0, high_total_tax - 4 * 11_000))


def test_quarterly_estimated_tax_high_agi_safe_harbor_reduces_year_end_by_4k() -> None:
    """Holding actual current-year tax fixed, flipping the safe-harbor from
    100% to 110% (via the MFS $75k AGI threshold trick: same $100k ordinary
    income, but MFS pushes it above the high-AGI threshold) trades $4k of
    year-end residual for $4k of estimated payments. This isolates the safe-
    harbor multiplier from bracket-induced differences in the underlying tax
    bill.
    """
    horizon_months = 13

    def _scenario_with_status(scenario_id: str, *, filing_status: TaxFilingStatus) -> Scenario:
        return Scenario(
            scenario_id=scenario_id,
            label=scenario_id.replace("_", " ").title(),
            actors=(_simple_actor(),),
            tax_profile=TaxProfile(
                filing_status=filing_status,
                # $100k ordinary income — below $150k (single/MFJ/HoH threshold)
                # but above $75k (MFS threshold). Flipping the filing status
                # therefore flips low/high AGI without changing the income or
                # the resulting tax brackets meaningfully.
                annual_ordinary_income_usd=100_000,
                prior_year_tax_usd=40_000,
            ),
            initial_balance_sheet=InitialBalanceSheet(
                accounts=(
                    AccountBalance(
                        account_id="checking",
                        account_type=AccountType.CHECKING,
                        owner_actor_id="alpha",
                        balance_usd=1_000_000,
                    ),
                )
            ),
        )

    # Standard 100% safe-harbor — $10k per quarter.
    low_result = _run_scenario(
        _scenario_with_status("estimated_tax_safe_harbor_low", filing_status=TaxFilingStatus.SINGLE),
        horizon_months=horizon_months,
    )
    low_estimated = _estimated_tax_obligations(low_result)
    for obligation in low_estimated:
        assert_allclose(obligation.amount_due_usd, 10_000)

    # 110% safe-harbor via MFS (which uses the $75k threshold) — $11k per quarter.
    high_result = _run_scenario(
        _scenario_with_status(
            "estimated_tax_safe_harbor_high", filing_status=TaxFilingStatus.MARRIED_FILING_SEPARATELY
        ),
        horizon_months=horizon_months,
    )
    high_estimated = _estimated_tax_obligations(high_result)
    for obligation in high_estimated:
        assert_allclose(obligation.amount_due_usd, 11_000)

    # The actual current-year tax differs slightly between filing statuses
    # (different standard deductions and bracket tables), so compare only
    # estimated-payment totals — the $4k delta is the safe-harbor difference.
    low_estimated_total = sum(obligation.amount_due_usd for obligation in low_estimated)
    high_estimated_total = sum(obligation.amount_due_usd for obligation in high_estimated)
    assert_allclose(high_estimated_total - low_estimated_total, 4_000)


def test_quarterly_estimated_tax_unfundable_q2_fails_rollout() -> None:
    """An estimated quarterly payment that can't be covered by cash or by any
    funding policy fails the rollout in the obligation's due month with
    `RolloutStatusType.FAILED` plus a `FailureEvent` keyed to
    `ESTIMATED_TAX_PAYMENT`.
    """
    horizon_months = 13
    scenario = Scenario(
        scenario_id="estimated_tax_unfundable",
        label="Estimated Tax Unfundable",
        actors=(_simple_actor(),),
        # Large prior-year tax → each quarter requires $50k. Cash starts at
        # $60k — Q1 settles partially (depleting cash to ~$10k), and Q2 at
        # month 5 can't be funded. No funding policy attached → FAILED.
        tax_profile=TaxProfile(prior_year_tax_usd=200_000),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=60_000
                ),
            )
        ),
    )
    result = _run_scenario(scenario, horizon_months=horizon_months)

    estimated = _estimated_tax_obligations(result)
    assert {obligation.month_index for obligation in estimated} == {3, 5, 8, 12}
    # Q1 at month 3: paid $50k from $60k cash → $10k cash left, status PAID.
    # Q2 at month 5: $10k cash, $50k due → PARTIALLY_PAID with $40k unpaid.
    # Q3/Q4 follow with 0 cash and full $50k unpaid each → UNPAID.
    by_month = {obligation.month_index: obligation for obligation in estimated}
    assert by_month[3].status is ObligationStatus.PAID
    assert by_month[5].status is ObligationStatus.PARTIALLY_PAID
    assert by_month[5].unpaid_amount_usd == pytest.approx(40_000)

    # Failure event for Q2 (first quarterly to fail) records the rollout failure.
    assert result.arrays is not None
    failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("estimated_tax_payment")
    )
    # One failure per partially or fully unpaid quarterly (Q2, Q3, Q4 here).
    assert len(failures) == 3
    assert {event.month_index for event in failures} == {5, 8, 12}
    q2_failure = next(event for event in failures if event.month_index == 5)
    assert q2_failure.unpaid_amount_usd == pytest.approx(40_000)

    status = result.rollout(0).status()
    assert status.status is RolloutStatusType.FAILED
    # First failed obligation lands at month 5 (Q2).
    assert status.first_failed_obligation_month_index == 5


def test_quarterly_estimated_tax_horizon_clip_drops_q3_q4_and_year_end() -> None:
    """With a 6-month horizon (Jan-Jun), only Q1 (Apr 15) and Q2 (Jun 15) fall
    inside; Q3, Q4, and the Dec 31 year-end land past the horizon and are
    dropped. Quarterly obligations are dropped entirely (unlike year-end,
    which the existing engine clips to the last in-horizon month).
    """
    horizon_months = 6
    scenario = Scenario(
        scenario_id="estimated_tax_horizon_clip",
        label="Estimated Tax Horizon Clip",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(prior_year_tax_usd=40_000),
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
    result = _run_scenario(scenario, horizon_months=horizon_months)
    estimated = _estimated_tax_obligations(result)
    # Q1 (month 3) and Q2 (month 5) only. Q3 at month 8 and Q4 at month 12 are
    # past horizon 6 and dropped.
    assert {obligation.month_index for obligation in estimated} == {3, 5}
    for obligation in estimated:
        assert_allclose(obligation.amount_due_usd, 10_000)
    # Year-end at month 11 is past horizon too; the existing engine clips it
    # to the last in-horizon month (here month 6). With no taxable income
    # in this scenario, no year-end obligation accrues either.
    annual = _annual_tax_obligations(result)
    assert annual == ()


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
            property_id="test_property", location_id="vallejo_ca", purchase_price_usd=purchase_price
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

    assert_allclose(rollout.series(ReportMetric.PARTNER_CONTRIBUTION_USD)[0], 0)
    assert_allclose(rollout.series(ReportMetric.PARTNER_CONTRIBUTION_USD)[1:], 1_000)
    assert np.all(rollout.series(ReportMetric.PARTNER_UNALLOCATED_EXCESS_USD)[1:] > 0)
    assert np.all(rollout.series(ReportMetric.PARTNER_HOUSE_COSTS_USD)[1:] > monthly_principal)
    assert_allclose(rollout.series(ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD)[1:], monthly_principal)
    assert_allclose(rollout.series(ReportMetric.OWNER_PRINCIPAL_CREDIT_USD)[1:], 0)
    assert_allclose(rollout.series(ReportMetric.PARTNER_EQUITY_LEDGER_USD)[60], expected_partner_ledger)
    assert_allclose(rollout.series(ReportMetric.OWNER_EQUITY_LEDGER_USD)[60], expected_owner_ledger)
    assert_allclose(rollout.series(ReportMetric.PARTNER_OWNERSHIP_PCT)[60], expected_ownership_pct)
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[60], expected_terminal_mortgage_balance)
    assert_allclose(rollout.series(ReportMetric.HOME_EQUITY_USD)[60], expected_home_equity)
    assert_allclose(rollout.series(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD)[60], expected_partner_ledger)
    assert_allclose(rollout.series(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD)[60], expected_owner_ledger)
    assert_allclose(
        rollout.series(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD)[60]
        + rollout.series(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD)[60],
        expected_home_equity,
    )
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[0], 20_000)
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[60], 20_000)
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER, side=PostingSide.CREDIT),
        result.matrix(ReportMetric.PARTNER_CONTRIBUTION_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_CONTRIBUTION_USED, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.PARTNER_CONTRIBUTION_USED_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_UNALLOCATED_CLAIM, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.PARTNER_UNALLOCATED_EXCESS_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.OWNER_PRINCIPAL_CREDIT, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.OWNER_PRINCIPAL_CREDIT_USD),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.PARTNER_EQUITY_LEDGER),
        result.matrix(ReportMetric.PARTNER_EQUITY_LEDGER_USD),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.OWNER_EQUITY_LEDGER),
        result.matrix(ReportMetric.OWNER_EQUITY_LEDGER_USD),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM),
        result.matrix(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD),
    )
    assert_allclose(
        _balance_snapshot_matrix(result, role=ChartAccountRole.OWNER_HOME_EQUITY_CLAIM),
        result.matrix(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD),
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

    # Partner contributions, mortgage payments, and partner-equity accruals are
    # canonicalized in ledger postings (PARTNER_CONTRIBUTION_TRANSFER,
    # MORTGAGE_PAYABLE, PARTNER_PRINCIPAL_CREDIT) and PARTNER_EQUITY_LEDGER
    # balance snapshots. The PartnerContributionDecision row carries the actor
    # decision trace; no standalone action row duplicates the accounting moves.
    contribution_decisions = result.policy_decisions(PartnerContributionDecision)
    assert len(contribution_decisions) == horizon_months
    assert contribution_decisions[0].actor_id == "beta"
    assert contribution_decisions[0].recipient_actor_id == "alpha"
    assert contribution_decisions[0].requested_amount_usd == 1_000

    # Mortgage principal accumulates against MORTGAGE_PAYABLE; the partner
    # ownership/claim flow against the snapshot ledgers.
    assert_allclose(rollout.series(ReportMetric.MORTGAGE_PRINCIPAL_USD)[1], monthly_principal)
    assert_allclose(
        rollout.series(ReportMetric.MORTGAGE_BALANCE_USD)[horizon_months], expected_terminal_mortgage_balance
    )
    assert_allclose(rollout.series(ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD)[1], monthly_principal)
    assert_allclose(rollout.series(ReportMetric.PARTNER_OWNERSHIP_PCT)[horizon_months], expected_ownership_pct)
    assert_allclose(rollout.series(ReportMetric.PARTNER_EQUITY_LEDGER_USD)[horizon_months], expected_partner_ledger)


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
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
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
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_GROSS_USD)[60], sale_value)
    assert_allclose(rollout.series(ReportMetric.SALE_CLOSING_COST_USD)[60], sale_closing_cost)
    assert_allclose(rollout.series(ReportMetric.REALIZED_PROPERTY_GAIN_USD)[60], realized_gain)
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_USD)[60], realized_gain)
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_EXCLUSION_USD)[60], 250_000)
    assert_allclose(rollout.series(ReportMetric.TAXABLE_PROPERTY_CAPITAL_GAIN_USD)[60], taxable_gain)
    assert_allclose(rollout.series(ReportMetric.TAXABLE_PROPERTY_GAIN_USD)[60], taxable_gain)
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_TAX_USD)[60], sale_tax)
    assert_allclose(rollout.series(ReportMetric.FEDERAL_INCOME_TAX_USD)[60], federal_sale_tax)
    assert_allclose(rollout.series(ReportMetric.CALIFORNIA_INCOME_TAX_USD)[60], california_sale_tax)
    # Net proceeds report the cash actually received at the sale event (pre-tax).
    # Sale tax accrues to the source month and settles at year-end via the
    # annual-tax obligation pipeline, not from the sale-event journal entry.
    pretax_net_proceeds = sale_value - sale_closing_cost
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD)[60], pretax_net_proceeds)
    assert_allclose(
        result.matrix(ReportMetric.NET_PROPERTY_SALE_CASH_FLOW_USD),
        result.matrix(ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PROPERTY,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix(ReportMetric.PROPERTY_SALE_GROSS_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix(ReportMetric.SALE_CLOSING_COST_USD),
    )
    # The property-sale journal entry no longer posts to TAX_EXPENSE.
    assert (
        _posting_matrix(
            result,
            role=ChartAccountRole.TAX_EXPENSE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ).sum()
        == 0
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
        result.matrix(ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "adjusted_basis_usd"),
        result.matrix(ReportMetric.PROPERTY_SALE_ADJUSTED_BASIS_USD),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "realized_gain_usd"),
        result.matrix(ReportMetric.REALIZED_PROPERTY_GAIN_USD),
    )
    assert_allclose(
        _accounting_detail_matrix(result, PropertySaleBasisGainDetail, "taxable_gain_usd"),
        result.matrix(ReportMetric.TAXABLE_PROPERTY_GAIN_USD),
    )
    assert_allclose(
        _accounting_detail_matrix(result, TaxPaymentAllocationDetail, "federal_income_tax_usd"),
        result.matrix(ReportMetric.FEDERAL_INCOME_TAX_USD),
    )
    assert_allclose(
        _accounting_detail_matrix(result, TaxPaymentAllocationDetail, "california_income_tax_usd"),
        result.matrix(ReportMetric.CALIFORNIA_INCOME_TAX_USD),
    )
    tax_details = rollout.accounting_details(TaxPaymentAllocationDetail)
    assert len(tax_details) == 1
    assert tax_details[0].payment_timing == TaxPaymentTiming.YEAR_END
    assert_allclose(tax_details[0].property_sale_tax_usd, sale_tax)
    sale_accounting_details = rollout.accounting_details(PropertySaleBasisGainDetail)
    assert len(sale_accounting_details) == 1
    assert_allclose(sale_accounting_details[0].adjusted_basis_usd, 500_000)
    assert_allclose(sale_accounting_details[0].taxable_gain_usd, taxable_gain)
    effects = rollout.effects(SettlePropertySaleEffect)
    assert len(effects) == 1
    effect = effects[0]
    assert effect.event_id == "sale"
    assert effect.property_id == "test_property"
    assert effect.policy_id == "property_sale_settlement"
    assert_allclose(effect.gross_sale_usd, sale_value)
    assert_allclose(effect.selling_cost_usd, sale_closing_cost)
    assert_allclose(effect.debt_payoff_usd, 0)
    assert_allclose(effect.adjusted_basis_usd, 500_000)
    assert_allclose(effect.realized_gain_usd, realized_gain)
    assert_allclose(effect.capital_gain_exclusion_usd, 250_000)
    assert_allclose(effect.taxable_gain_usd, taxable_gain)
    assert_allclose(effect.tax_usd, sale_tax)
    assert_allclose(effect.net_proceeds_usd, pretax_net_proceeds)


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
            property_id="test_property", location_id="vallejo_ca", purchase_price_usd=purchase_price
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
    sale_effect = rollout.effects(SettlePropertySaleEffect)[0]
    sale_net_proceeds = sale_effect.net_proceeds_usd
    ownership_pct = rollout.series(ReportMetric.PARTNER_OWNERSHIP_PCT)[sale_month]
    expected_partner_claim = sale_net_proceeds * ownership_pct
    expected_owner_claim = sale_net_proceeds - expected_partner_claim
    gross_equity_claim = rollout.series(ReportMetric.HOME_EQUITY_USD)[sale_month] * ownership_pct

    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD)[sale_month], sale_net_proceeds)
    assert_allclose(rollout.series(ReportMetric.PROPERTY_SALE_DEBT_PAYOFF_USD)[sale_month], sale_effect.debt_payoff_usd)
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.MORTGAGE_PAYABLE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
        ),
        result.matrix(ReportMetric.PROPERTY_SALE_DEBT_PAYOFF_USD),
    )
    assert sale_net_proceeds < rollout.series(ReportMetric.HOME_EQUITY_USD)[sale_month]
    assert not np.isclose(expected_partner_claim, gross_equity_claim)
    assert_allclose(rollout.series(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD)[sale_month], expected_partner_claim)
    assert_allclose(rollout.series(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD)[sale_month], expected_owner_claim)
    assert_allclose(
        rollout.series(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD)[sale_month]
        + rollout.series(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD)[sale_month],
        sale_net_proceeds,
    )
    assert_allclose(rollout.series(ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD)[4], expected_partner_claim)
    assert_allclose(rollout.series(ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD)[4], expected_owner_claim)

    # The partner-equity home claim after the sale is reported by the
    # PARTNER_HOME_EQUITY_CLAIM monthly metric (asserted above) which derives
    # from the OWNERSHIP_PARTNER_HOME_EQUITY_CLAIM balance snapshots.


def test_simulate_set_response_serializes_sale_effects_with_tax_detail() -> None:
    """The public response payload preserves per-rollout effect details for UI inspection."""
    scenario = Scenario(
        scenario_id="serialized_sale_effects",
        label="Serialized Sale Effects",
        actors=(_simple_actor(),),
        events=(PropertySaleEvent(event_id="property_sale", month_index=2, property_id="test_property"),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=500_000
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
        scenario_set_id="serialized_sale_effects_set",
        title="Serialized Sale Effects Set",
        market_request=MarketRequest(market_model_id="e2e_noop", rollout_count=1, horizon_months=2, seed=0),
        scenarios=(scenario,),
    )

    run = simulate_set(
        scenario_set,
        market_provider=NoopMarketBundleProvider(
            home_path=(1.0, 1.0, 1.8), private_equity_sale_opportunity_months=(1,)
        ),
        local_regulation_by_id=_TEST_LOCAL_REGULATION_BY_ID,
    )
    payload = run.to_response().model_dump(mode="json")

    result = payload["scenario_results"][0]
    assert {"federal_income_tax_usd", "california_income_tax_usd", "generic_sp500_sale_tax_usd"} <= set(
        result["monthly_columns"]["columns"]
    )
    effects = {effect["effect_type"]: effect for effect in result["effects"]}
    assert set(effects) == {"sell_sp500", "sell_private_equity", "settle_property_sale"}
    # `chart_accounts` / `postings` / `journal_entries` / `balance_snapshots`
    # are no longer shipped on the wire (frontend doesn't read them); read
    # them off the in-memory ScenarioRun instead.
    scenario_run = run.scenario_runs[0]
    posting_roles = {posting.chart_account_id.split(":", 1)[0] for posting in scenario_run.postings()}
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

    sp500_effect = effects["sell_sp500"]
    assert sp500_effect["amount_usd"] == 20_000
    assert sp500_effect["basis_usd"] == 10_000
    assert sp500_effect["gain_usd"] == 10_000
    assert sp500_effect["tax_usd"] > 0
    assert_allclose(sp500_effect["after_tax_proceeds_usd"], sp500_effect["amount_usd"] - sp500_effect["tax_usd"])

    private_equity_effect = effects["sell_private_equity"]
    assert private_equity_effect["event_id"] is None
    assert private_equity_effect["event_type"] is None
    assert private_equity_effect["opportunity_id"] == (
        f"{private_equity_effect['path_set_id']}:path:0:month:1:private_equity_holding:pe:sale_opportunity"
    )
    assert private_equity_effect["opportunity_cause_id"] == private_equity_effect["opportunity_id"]
    assert private_equity_effect["amount_usd"] == 50_000
    assert private_equity_effect["basis_usd"] == 20_000
    assert private_equity_effect["taxable_gain_usd"] == 30_000
    assert private_equity_effect["estimated_tax_usd"] > 0
    assert_allclose(
        private_equity_effect["after_tax_proceeds_usd"],
        private_equity_effect["amount_usd"] - private_equity_effect["estimated_tax_usd"],
    )

    property_effect = effects["settle_property_sale"]
    assert property_effect["event_id"] == "property_sale"
    assert property_effect["property_id"] == "test_property"
    assert property_effect["gross_sale_usd"] == 900_000
    assert property_effect["selling_cost_usd"] == 58_500
    assert property_effect["adjusted_basis_usd"] == 500_000
    assert property_effect["taxable_capital_gain_usd"] == 91_500
    assert property_effect["tax_usd"] > 0
    # net_proceeds is the cash actually received at the sale event (pre-tax);
    # sale tax accrues to the sale month for provenance and settles at year-end.
    assert_allclose(
        property_effect["net_proceeds_usd"],
        property_effect["gross_sale_usd"] - property_effect["selling_cost_usd"] - property_effect["debt_payoff_usd"],
    )


def test_whole_property_rental_posts_income_fees_and_cash_flow() -> None:
    """A rented property records rent, vacancy, management fee, carrying cost,
    and owner cash impact in the simulated trajectory."""
    scenario = Scenario(
        scenario_id="whole_property_rental",
        label="Whole Property Rental",
        actors=(_simple_actor(),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="vallejo_ca", purchase_price_usd=120_000
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
    assert_allclose(rollout.series(ReportMetric.RENTAL_INCOME_USD)[0], 0)
    assert_allclose(rollout.series(ReportMetric.RENTAL_INCOME_USD)[1], expected_rental_income)
    assert_allclose(rollout.series(ReportMetric.RENTAL_MANAGEMENT_FEE_USD)[1], expected_management_fee)
    assert_allclose(rollout.series(ReportMetric.PROPERTY_TAX_USD)[1], expected_property_tax)
    assert_allclose(
        rollout.series(ReportMetric.PROPERTY_CARRYING_COST_USD)[1], expected_management_fee + expected_property_tax
    )
    assert_allclose(rollout.series(ReportMetric.NET_PROPERTY_CASH_FLOW_USD)[1], expected_net_property_cash_flow)
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[0], 130_000)
    # Positive net rental income produces a CA ordinary-income tax obligation.
    # The annual-tax pipeline accrues tax to the source month for provenance
    # (RENTAL_INCOME_TAX_USD reports per-month attribution) but settles the
    # obligation at year-end. The simulation horizon is shorter than a year
    # here, so the obligation collapses onto the last in-horizon month — the
    # source-month cash trajectory is unaffected.
    rental_tax_month_1 = rollout.series(ReportMetric.RENTAL_INCOME_TAX_USD)[1]
    assert rental_tax_month_1 > 0
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[1], 130_000 + expected_net_property_cash_flow)
    # Tax for year 0 settles at the last in-horizon month belonging to year 0
    # (here, month 3 = horizon end).
    total_rental_tax_year_0 = float(np.sum(rollout.series(ReportMetric.RENTAL_INCOME_TAX_USD)))
    assert total_rental_tax_year_0 > 0
    assert_allclose(
        rollout.series(ReportMetric.CASH_USD)[3],
        130_000 + 3 * expected_net_property_cash_flow - total_rental_tax_year_0,
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.RENTAL_INCOME, side=PostingSide.CREDIT),
        result.matrix(ReportMetric.RENTAL_INCOME_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.PROPERTY_OPERATING,
        ),
        result.matrix(ReportMetric.RENTAL_INCOME_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.PROPERTY_TAX_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.PROPERTY_TAX_USD),
    )
    assert_allclose(
        _posting_matrix(result, role=ChartAccountRole.RENTAL_MANAGEMENT_FEE_EXPENSE, side=PostingSide.DEBIT),
        result.matrix(ReportMetric.RENTAL_MANAGEMENT_FEE_USD),
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
        # prior_year_tax_usd=0 opts out of quarterly estimated payments so the
        # asserted cash trajectory below sees only the year-end true-up.
        tax_profile=TaxProfile(prior_year_tax_usd=0),
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
    # Sale tax is now accrued in the source month (month 5) but settles at year-end.
    # The simulation horizon ends inside year 0, so the obligation collapses onto
    # the last in-horizon month (month 6) instead of the sale month.
    assert_allclose(
        rollout.series(ReportMetric.CASH_USD),
        [30_000, 25_000, 20_000, 15_000, 10_000, 25_000, 20_000 - expected_stock_sale_tax],
    )
    assert_allclose(
        rollout.series(ReportMetric.GENERIC_SP500_VALUE_USD), [50_000, 50_000, 50_000, 50_000, 50_000, 30_000, 30_000]
    )
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_USD), [0, 0, 0, 0, 0, 20_000, 0])
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_BASIS_USD)[5], 10_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_GAIN_USD)[5], 10_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_TAX_USD)[5], expected_stock_sale_tax)
    assert_allclose(rollout.series(ReportMetric.CHECKING_FLOOR_SHORTFALL_USD), 0)
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.PUBLIC_SECURITY,
            side=PostingSide.CREDIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        ),
        result.matrix(ReportMetric.GENERIC_SP500_SALE_USD),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PUBLIC_SECURITY, amount_field="cost_basis_usd"),
        result.matrix(ReportMetric.GENERIC_SP500_SALE_BASIS_USD),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PUBLIC_SECURITY, amount_field="tax_expense_usd"),
        result.matrix(ReportMetric.GENERIC_SP500_SALE_TAX_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        )
        - result.matrix(ReportMetric.GENERIC_SP500_SALE_TAX_USD),
        result.matrix(ReportMetric.GENERIC_SP500_SALE_USD) - result.matrix(ReportMetric.GENERIC_SP500_SALE_TAX_USD),
    )

    effects = result.effects(SellSp500Effect)
    assert len(effects) == 1
    assert effects[0].month_index == 5
    assert effects[0].policy_id == "checking_floor"
    assert effects[0].amount_usd == 20_000
    assert_allclose(effects[0].after_tax_proceeds_usd, 20_000 - expected_stock_sale_tax)
    assert effects[0].basis_usd == 10_000
    assert effects[0].gain_usd == 10_000
    assert_allclose(effects[0].tax_usd, expected_stock_sale_tax)
    assert effects[0].shortfall_usd == 0
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
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[0], 25_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_VALUE_USD)[0], 25_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_USD)[0], 25_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_BASIS_USD)[0], 25_000)
    assert_allclose(rollout.series(ReportMetric.CHECKING_FLOOR_SHORTFALL_USD)[0], 0)

    assert [(effect.policy_id, effect.amount_usd) for effect in result.effects(SellSp500Effect)] == [
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
    assert_allclose(no_opportunity.rollout(0).series(ReportMetric.PRIVATE_EQUITY_SALE_USD), 0)
    assert_allclose(no_opportunity.rollout(0).series(ReportMetric.CASH_USD)[12], 10_000)
    assert_allclose(no_opportunity.rollout(0).series(ReportMetric.LIQUID_NET_WORTH_USD)[12], 10_000)
    assert no_opportunity.effects(SellPrivateEquityEffect) == ()
    no_opportunity_decisions = no_opportunity.policy_decisions(PrivateEquitySaleDecision)
    assert len(no_opportunity_decisions) == 13
    assert {decision.decision_reason for decision in no_opportunity_decisions} == {
        PrivateEquitySaleDecisionReason.NO_SALE_OPPORTUNITY
    }
    assert {decision.source_asset_id for decision in no_opportunity_decisions} == {"pe"}
    assert {decision.sale_rule_type for decision in no_opportunity_decisions} == {
        PrivateEquitySaleRuleType.FIXED_AMOUNT_ON_OPPORTUNITY
    }
    assert {decision.configured_sale_amount_usd for decision in no_opportunity_decisions} == {100_000}
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
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_VALUE_USD)[11], 200_000)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)[12], expected_sale)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD)[12], expected_basis)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD)[12], expected_tax)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_VALUE_USD)[12], 100_000)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_VALUE_USD)[12], 100_000)
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[12], 10_000 + expected_after_tax_proceeds)
    assert_allclose(rollout.series(ReportMetric.LIQUID_NET_WORTH_USD)[12], 10_000 + expected_after_tax_proceeds)
    assert_allclose(rollout.series(ReportMetric.NET_WORTH_USD)[12], 10_000 + expected_after_tax_proceeds + 100_000)
    opportunity_observations = rollout.market_observations(PrivateEquitySaleOpportunityObservation)
    assert len(opportunity_observations) == 1
    assert opportunity_observations[0].month_index == 12
    assert opportunity_observations[0].source_asset_id == "pe"
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
    assert pe_decision.source_asset_id == "pe"
    assert pe_decision.sale_rule_type is PrivateEquitySaleRuleType.FIXED_AMOUNT_ON_OPPORTUNITY
    assert pe_decision.configured_sale_amount_usd == 100_000
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
        result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_USD),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PRIVATE_EQUITY, amount_field="cost_basis_usd"),
        result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD),
    )
    assert_allclose(
        _lot_disposition_matrix(result, asset_class=LotAssetClass.PRIVATE_EQUITY, amount_field="tax_expense_usd"),
        result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD),
    )
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.CHECKING_CASH,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.ASSET_SALE,
        )
        - result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD),
        result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_USD) - result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD),
    )

    effects = result.effects(SellPrivateEquityEffect)
    assert len(effects) == 1
    assert effects[0].month_index == 12
    assert effects[0].event_id is None
    assert effects[0].event_type is None
    assert effects[0].opportunity_id == expected_opportunity_id
    assert effects[0].opportunity_cause_id == expected_opportunity_id
    assert effects[0].actor_id == "alpha"
    assert effects[0].policy_id == "private_equity_sale"
    assert effects[0].amount_usd == expected_sale
    assert effects[0].basis_usd == expected_basis
    assert effects[0].taxable_gain_usd == expected_taxable_gain
    assert_allclose(effects[0].estimated_tax_usd, expected_tax)
    assert_allclose(effects[0].after_tax_proceeds_usd, expected_after_tax_proceeds)
    assert effects[0].units_sold == 50
    assert effects[0].sold_fraction == 0.5
    assert effects[0].proceeds_destination is AccountType.CHECKING


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
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)[6], 50_000)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD)[6], 20_000)
    expected_tax = 375.09
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD)[6], expected_tax)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_VALUE_USD)[6], 150_000)
    assert_allclose(rollout.series(ReportMetric.CASH_USD)[6], 60_000 - expected_tax)
    effects = result.effects(SellPrivateEquityEffect)
    assert len(effects) == 1
    assert effects[0].event_id is None
    assert effects[0].event_type is None
    assert effects[0].amount_usd == 50_000
    assert_allclose(effects[0].after_tax_proceeds_usd, 50_000 - expected_tax)


def test_public_market_pe_position_sells_freely_each_month_via_pe_sale_policy() -> None:
    """A `PublicMarket`-regime PE position is sellable every month at the
    spot mark — no tender opportunity needed. The `PrivateEquitySalePolicy`
    therefore fires every month the rule triggers, draining the position
    over time without needing the market provider to emit
    `private_equity_sale_opportunity_months`.
    """
    scenario = Scenario(
        scenario_id="public_market_pe_no_lockup",
        label="PublicMarket PE No Lockup",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=0
                ),
            ),
            assets=(
                PrivateEquityPosition(
                    asset_id="pe",
                    owner_actor_id="alpha",
                    value_usd=600_000,
                    cost_basis_usd=600_000,
                    units=600,
                    liquidity_regime=PublicMarket(),
                ),
            ),
        ),
        policies=(
            PrivateEquitySalePolicy(
                policy_id="pe_sale",
                actor_id="alpha",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=50_000),
            ),
        ),
    )

    # Default provider: no tender months. With LiquidityEventOnly this would
    # produce no sales; PublicMarket overrides the mask to every month
    # (lockup_end_month=None ⇒ sellable from month 0).
    result = _run_scenario(scenario, horizon_months=6)
    rollout = result.rollout(0)
    pe_sales = rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)
    for month in range(7):
        assert_allclose(pe_sales[month], 50_000)
    # Basis equals proceeds here (cost_basis == value_usd), so no taxable gain.
    assert_allclose(np.sum(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD)), 7 * 50_000)
    assert_allclose(np.sum(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD)), 0)


def test_public_market_pe_lockup_blocks_sale_before_lockup_end_month() -> None:
    """With `lockup_end_month=4`, a `PublicMarket` PE position cannot be sold
    in months [0, 4); from month 4 onward sales fire normally. The
    `PrivateEquitySalePolicy` opportunity check honors the effective mask.
    """
    scenario = Scenario(
        scenario_id="public_market_pe_with_lockup",
        label="PublicMarket PE With Lockup",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=0
                ),
            ),
            assets=(
                PrivateEquityPosition(
                    asset_id="pe",
                    owner_actor_id="alpha",
                    value_usd=200_000,
                    cost_basis_usd=200_000,
                    units=200,
                    liquidity_regime=PublicMarket(lockup_end_month=4),
                ),
            ),
        ),
        policies=(
            PrivateEquitySalePolicy(
                policy_id="pe_sale",
                actor_id="alpha",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=25_000),
            ),
        ),
    )

    result = _run_scenario(scenario, horizon_months=6)
    rollout = result.rollout(0)
    pe_sales = rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)
    # Pre-lockup-end months (0..3) must show no sale.
    for month in range(4):
        assert pe_sales[month] == 0, f"month {month} should be in lockup"
    # Post-lockup months (4..6) sell.
    for month in range(4, 7):
        assert_allclose(pe_sales[month], 25_000)


def test_acquisition_regime_forces_full_conversion_at_event_month() -> None:
    """An `Acquisition`-regime PE position converts the entire remaining
    position to cash at `event_month` regardless of any policy. Cash
    increases by `units × cash_per_unit_usd`; PE units drop to zero;
    realized gain feeds the existing annual sale-tax allocation.
    """
    scenario = Scenario(
        scenario_id="pe_acquisition_event",
        label="PE Acquisition Event",
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
                    owner_actor_id="alpha",
                    value_usd=100_000,
                    cost_basis_usd=40_000,
                    units=100,
                    liquidity_regime=Acquisition(event_month=6, cash_per_unit_usd=500),
                ),
            ),
        ),
        # No PE sale policy: the acquisition is a forced conversion.
    )

    result = _run_scenario(scenario, horizon_months=12)
    rollout = result.rollout(0)
    pe_sales = rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)
    pe_value = rollout.series(ReportMetric.PRIVATE_EQUITY_VALUE_USD)
    cash = rollout.series(ReportMetric.CASH_USD)
    expected_proceeds = 100 * 500  # 50_000
    expected_basis = 40_000
    expected_taxable_gain = 10_000
    assert_allclose(pe_sales[6], expected_proceeds)
    for month in (0, 1, 5):
        assert pe_sales[month] == 0
    for month in (7, 8, 12):
        assert pe_sales[month] == 0
    for month in (6, 7, 12):
        assert_allclose(pe_value[month], 0)
    # Sale tax is recorded at the source month by `annual_sale_tax_allocation`
    # for visibility. Tax cash settlement happens via the quarterly estimated
    # tax obligations (months 3/5/8/12) plus a year-end true-up at month 11,
    # so cash[6] is full proceeds minus the Q1+Q2 estimated payments and
    # cash[12] is full proceeds minus the full annual tax.
    pe_sale_tax_month_6 = rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD)[6]
    assert pe_sale_tax_month_6 > 0
    # The acquisition proceeds are received in cash at month 6 — verify the
    # cash jumped by at least most of the proceeds (allowing for already-paid
    # quarterly estimated taxes).
    assert cash[6] > 10_000 + expected_proceeds - 1_000, f"cash[6]={cash[6]}"
    # Year-end (month 12) cash reflects full proceeds minus the full sale tax.
    assert_allclose(cash[12], 10_000 + expected_proceeds - pe_sale_tax_month_6, atol=1.0)
    # Verify the SellPrivateEquityEffect carries the expected basis/gain.
    effects = result.effects(SellPrivateEquityEffect)
    assert len(effects) == 1
    assert effects[0].month_index == 6
    assert effects[0].amount_usd == expected_proceeds
    assert effects[0].basis_usd == expected_basis
    assert effects[0].taxable_gain_usd == expected_taxable_gain
    assert effects[0].units_sold == 100
    assert effects[0].sold_fraction == 1.0


def test_acquisition_regime_short_holding_period_still_recognizes_realized_gain() -> None:
    """The current `annual_sale_tax_allocation` treats all PE realized gain
    as long-term capital gain (LT/ST partitioning is not modeled). This test
    pins that behavior: an acquisition at month 3 (holding < 12 months)
    produces a realized gain that flows through the same LT path as one at
    month 24 — so for the same gain, the recorded sale tax matches the
    long-term-treatment tax. When LT/ST partitioning lands, this test will
    have to grow accordingly.
    """
    scenario_short = Scenario(
        scenario_id="acquisition_short_holding",
        label="Acquisition Short Holding",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(annual_ordinary_income_usd=80_000),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=0
                ),
            ),
            assets=(
                PrivateEquityPosition(
                    asset_id="pe",
                    owner_actor_id="alpha",
                    value_usd=100_000,
                    cost_basis_usd=20_000,
                    units=100,
                    liquidity_regime=Acquisition(event_month=3, cash_per_unit_usd=1_000),
                ),
            ),
        ),
    )
    scenario_long = scenario_short.model_copy(
        update={
            "scenario_id": "acquisition_long_holding",
            "initial_balance_sheet": InitialBalanceSheet(
                accounts=scenario_short.initial_balance_sheet.accounts,
                assets=(
                    PrivateEquityPosition(
                        asset_id="pe",
                        owner_actor_id="alpha",
                        value_usd=100_000,
                        cost_basis_usd=20_000,
                        units=100,
                        liquidity_regime=Acquisition(event_month=24, cash_per_unit_usd=1_000),
                    ),
                ),
            ),
        }
    )

    result_short = _run_scenario(scenario_short, horizon_months=36)
    result_long = _run_scenario(scenario_long, horizon_months=36)
    short_effects = result_short.effects(SellPrivateEquityEffect)
    long_effects = result_long.effects(SellPrivateEquityEffect)
    assert len(short_effects) == 1
    assert len(long_effects) == 1
    # Both record the same proceeds, basis, and taxable gain; LT treatment is the
    # existing engine convention, so the realized-gain tax is identical regardless
    # of holding period under today's `annual_sale_tax_allocation`.
    assert short_effects[0].amount_usd == long_effects[0].amount_usd == 100_000
    assert short_effects[0].basis_usd == long_effects[0].basis_usd == 20_000
    assert short_effects[0].taxable_gain_usd == long_effects[0].taxable_gain_usd == 80_000
    assert_allclose(short_effects[0].estimated_tax_usd, long_effects[0].estimated_tax_usd)


def test_every_monthly_flow_metric_reconciles_to_canonical_detail_surface() -> None:
    """Cleanup-audit item 2 invariant: every public monthly flow column is derived
    from the canonical detail surface (ledger postings, balance snapshots,
    accounting details, or market observations) — never from a parallel
    Effect recorder. This guard rebuilds each monthly metric matrix
    from the canonical detail rows the engine emits and asserts it equals the
    monthly array reported in the result.

    The scenario combines a partner-equity occupant, a mortgaged property with
    rental income, a checking-floor stock sale, a private-equity sale
    opportunity, monthly spend, and an end-of-horizon property sale so the
    reconciliation exercises every monthly column with a LEDGER_ENTRY,
    BALANCE_SNAPSHOT, ACCOUNTING_DETAIL, or MARKET_OBSERVATION source.

    Metrics whose source is TRAJECTORY_STATE (state arrays computed
    vectorially: cash, property value, mortgage balance, depreciation schedule,
    partner_present) or REPORT_PROJECTION (derived from other metrics: e.g.
    mortgage_payment_usd = interest + principal) are not in scope for this
    guard — they have no separate ledger/accounting source by construction.
    """
    purchase_price = 500_000
    sale_month = 12
    scenario = Scenario(
        scenario_id="reconciliation_guard",
        label="Reconciliation Guard",
        actors=(_simple_actor(), Actor(actor_id="beta", label="Beta", role=ActorRole.EQUITY_BUILDING_OCCUPANT)),
        events=(PropertySaleEvent(event_id="sale", month_index=sale_month, property_id="test_property"),),
        property_selection=PropertySelection(
            property_id="test_property", location_id="san_francisco_ca", purchase_price_usd=purchase_price
        ),
        financing=Financing(financing_mode=FinancingMode.FIXED_30, down_payment_pct=25, mortgage_rate_pct=6),
        transaction_costs=TransactionCosts(closing_cost_buy_pct=2.5, closing_cost_sell_pct=6.5),
        property_assumptions=PropertyAssumptions(insurance_annual_usd=1_800, maintenance_pct=1),
        rental_plan=WholePropertyRentalPlan(
            rental_mode=RentalMode.RENT_WHOLE_PROPERTY,
            start_month=1,
            end_month=sale_month - 1,
            monthly_rent_usd=3_500,
            vacancy_pct=5,
            management_fee_pct=8,
        ),
        tax_profile=TaxProfile(filing_status=TaxFilingStatus.SINGLE, annual_ordinary_income_usd=120_000),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=200_000,
                ),
            ),
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=100_000,
                    cost_basis_usd=50_000,
                ),
                PrivateEquityPosition(
                    asset_id="pe",
                    asset_type=AssetType.PRIVATE_EQUITY,
                    owner_actor_id="alpha",
                    value_usd=200_000,
                    cost_basis_usd=80_000,
                    units=200,
                ),
            ),
        ),
        policies=(
            MonthlySpendPolicy(policy_id="living_expenses", actor_id="alpha", monthly_spend_usd=2_000),
            CheckingFloorSellPublicStockPolicy(
                policy_id="checking_floor", actor_id="alpha", floor_usd=50_000, sale_amount_usd=10_000
            ),
            PrivateEquitySalePolicy(
                policy_id="pe_sale",
                actor_id="alpha",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=25_000),
            ),
            PartnerEquityAccrualPolicy(
                policy_id="partner_equity",
                actor_id="beta",
                property_id="test_property",
                base_monthly_payment_usd=800,
                grow_with_inflation=False,
                occupied_months=sale_month,
            ),
        ),
    )
    result = _run_scenario(
        scenario,
        rollout_count=2,
        horizon_months=sale_month,
        market_provider=NoopMarketBundleProvider(
            home_path=tuple(1.0 + 0.02 * month for month in range(sale_month + 1)),
            private_equity_sale_opportunity_months=(4,),
        ),
    )

    # Ledger-derived: each metric equals the sum of postings on the named role
    # (and journal entry type when the spec calls one out).
    ledger_reconciliations = (
        (ReportMetric.MONTHLY_SPEND_USD, ChartAccountRole.MONTHLY_LIVING_EXPENSE, PostingSide.DEBIT, None),
        (ReportMetric.MORTGAGE_INTEREST_USD, ChartAccountRole.MORTGAGE_INTEREST_EXPENSE, PostingSide.DEBIT, None),
        (
            ReportMetric.MORTGAGE_PRINCIPAL_USD,
            ChartAccountRole.MORTGAGE_PAYABLE,
            PostingSide.DEBIT,
            JournalEntryType.MORTGAGE_PAYMENT,
        ),
        (ReportMetric.PROPERTY_TAX_USD, ChartAccountRole.PROPERTY_TAX_EXPENSE, PostingSide.DEBIT, None),
        (ReportMetric.HOA_USD, ChartAccountRole.HOA_EXPENSE, PostingSide.DEBIT, None),
        (ReportMetric.INSURANCE_USD, ChartAccountRole.INSURANCE_EXPENSE, PostingSide.DEBIT, None),
        (ReportMetric.MAINTENANCE_USD, ChartAccountRole.MAINTENANCE_EXPENSE, PostingSide.DEBIT, None),
        (ReportMetric.RENTAL_INCOME_USD, ChartAccountRole.RENTAL_INCOME, PostingSide.CREDIT, None),
        (
            ReportMetric.RENTAL_MANAGEMENT_FEE_USD,
            ChartAccountRole.RENTAL_MANAGEMENT_FEE_EXPENSE,
            PostingSide.DEBIT,
            None,
        ),
        (ReportMetric.RENTAL_LEASING_FEE_USD, ChartAccountRole.RENTAL_LEASING_FEE_EXPENSE, PostingSide.DEBIT, None),
        (
            ReportMetric.SALE_CLOSING_COST_USD,
            ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
            PostingSide.DEBIT,
            JournalEntryType.PROPERTY_SALE,
        ),
        (
            ReportMetric.PROPERTY_SALE_GROSS_USD,
            ChartAccountRole.PROPERTY,
            PostingSide.CREDIT,
            JournalEntryType.PROPERTY_SALE,
        ),
        (
            ReportMetric.PROPERTY_SALE_DEBT_PAYOFF_USD,
            ChartAccountRole.MORTGAGE_PAYABLE,
            PostingSide.DEBIT,
            JournalEntryType.PROPERTY_SALE,
        ),
        (
            ReportMetric.GENERIC_SP500_SALE_USD,
            ChartAccountRole.PUBLIC_SECURITY,
            PostingSide.CREDIT,
            JournalEntryType.ASSET_SALE,
        ),
        (
            ReportMetric.PRIVATE_EQUITY_SALE_USD,
            ChartAccountRole.PRIVATE_EQUITY,
            PostingSide.CREDIT,
            JournalEntryType.ASSET_SALE,
        ),
        (
            ReportMetric.PARTNER_CONTRIBUTION_USD,
            ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER,
            PostingSide.CREDIT,
            None,
        ),
        (
            ReportMetric.PARTNER_CONTRIBUTION_USED_USD,
            ChartAccountRole.PARTNER_CONTRIBUTION_USED,
            PostingSide.DEBIT,
            None,
        ),
        (
            ReportMetric.PARTNER_UNALLOCATED_EXCESS_USD,
            ChartAccountRole.PARTNER_UNALLOCATED_CLAIM,
            PostingSide.DEBIT,
            None,
        ),
        (ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD, ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, PostingSide.DEBIT, None),
        (ReportMetric.OWNER_PRINCIPAL_CREDIT_USD, ChartAccountRole.OWNER_PRINCIPAL_CREDIT, PostingSide.DEBIT, None),
    )
    for metric, role, side, journal_entry_type in ledger_reconciliations:
        assert_allclose(
            _posting_matrix(result, role=role, side=side, journal_entry_type=journal_entry_type),
            result.matrix(metric),
            err_msg=f"{metric} should equal ledger postings on {role}/{side.value}",
        )

    # Lot-disposition-derived (sale-cost-basis + sale-tax attribution): the
    # canonical surface is the lot disposition row, which records cost basis
    # consumed and the per-sale tax expense allocation. These show up under
    # LEDGER_ENTRY in `_MONTHLY_COLUMN_SPECS` because lot dispositions are a
    # part of the accounting trace, not a parallel surface.
    lot_disposition_reconciliations = (
        (ReportMetric.GENERIC_SP500_SALE_BASIS_USD, LotAssetClass.PUBLIC_SECURITY, "cost_basis_usd"),
        (ReportMetric.GENERIC_SP500_SALE_TAX_USD, LotAssetClass.PUBLIC_SECURITY, "tax_expense_usd"),
        (ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD, LotAssetClass.PRIVATE_EQUITY, "cost_basis_usd"),
        (ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD, LotAssetClass.PRIVATE_EQUITY, "tax_expense_usd"),
        (ReportMetric.PROPERTY_SALE_TAX_USD, LotAssetClass.PROPERTY, "tax_expense_usd"),
    )
    for metric, asset_class, amount_field in lot_disposition_reconciliations:
        assert_allclose(
            _lot_disposition_matrix(result, asset_class=asset_class, amount_field=amount_field),
            result.matrix(metric),
            err_msg=f"{metric} should equal lot-disposition.{amount_field} for {asset_class}",
        )

    # Cash flow on the property-sale journal entry: net proceeds equal cash
    # debits minus cash credits on that journal entry.
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
        result.matrix(ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD),
    )

    # Balance-snapshot-derived: ownership ledgers and home equity claims.
    snapshot_reconciliations = (
        (ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD, ChartAccountRole.OWNER_HOME_EQUITY_CLAIM),
        (ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD, ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM),
        (ReportMetric.OWNER_EQUITY_LEDGER_USD, ChartAccountRole.OWNER_EQUITY_LEDGER),
        (ReportMetric.PARTNER_EQUITY_LEDGER_USD, ChartAccountRole.PARTNER_EQUITY_LEDGER),
    )
    for metric, role in snapshot_reconciliations:
        assert_allclose(
            _balance_snapshot_matrix(result, role=role),
            result.matrix(metric),
            err_msg=f"{metric} should equal balance-snapshot on {role}",
        )

    # Accounting-detail-derived: tax payment allocations and property-sale
    # basis/gain attribution.
    detail_reconciliations = (
        (ReportMetric.FEDERAL_INCOME_TAX_USD, TaxPaymentAllocationDetail, "federal_income_tax_usd"),
        (ReportMetric.CALIFORNIA_INCOME_TAX_USD, TaxPaymentAllocationDetail, "california_income_tax_usd"),
        (ReportMetric.TOTAL_INCOME_TAX_USD, TaxPaymentAllocationDetail, "total_income_tax_usd"),
        (ReportMetric.RENTAL_INCOME_TAX_USD, TaxPaymentAllocationDetail, "rental_income_tax_usd"),
        (ReportMetric.PROPERTY_SALE_ADJUSTED_BASIS_USD, PropertySaleBasisGainDetail, "adjusted_basis_usd"),
        (ReportMetric.REALIZED_PROPERTY_GAIN_USD, PropertySaleBasisGainDetail, "realized_gain_usd"),
        (ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_USD, PropertySaleBasisGainDetail, "capital_gain_usd"),
        (
            ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_EXCLUSION_USD,
            PropertySaleBasisGainDetail,
            "capital_gain_exclusion_usd",
        ),
        (ReportMetric.TAXABLE_PROPERTY_CAPITAL_GAIN_USD, PropertySaleBasisGainDetail, "taxable_capital_gain_usd"),
        (ReportMetric.TAXABLE_PROPERTY_GAIN_USD, PropertySaleBasisGainDetail, "taxable_gain_usd"),
        (ReportMetric.DEPRECIATION_RECAPTURE_USD, PropertySaleBasisGainDetail, "depreciation_recapture_usd"),
    )
    for metric, detail_type, amount_field in detail_reconciliations:
        assert_allclose(
            _accounting_detail_matrix(result, detail_type, amount_field),
            result.matrix(metric),
            err_msg=f"{metric} should equal accounting-detail {detail_type.__name__}.{amount_field}",
        )

    # Market-observation-derived: the PE sale opportunity event mask comes
    # from the MarketPathObservation rows the engine emits per rollout/month.
    market_observation_event_matrix = np.zeros_like(result.matrix(ReportMetric.CASH_USD), dtype=np.bool_)
    for observation in result.rollout(0).market_observations(MarketPathObservation):
        market_observation_event_matrix[0, observation.month_index] = observation.private_equity_sale_opportunity_event
    for observation in result.rollout(1).market_observations(MarketPathObservation):
        market_observation_event_matrix[1, observation.month_index] = observation.private_equity_sale_opportunity_event
    assert_allclose(
        market_observation_event_matrix.astype("float64"),
        result.matrix(ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_EVENT).astype("float64"),
    )


_PORTFOLIO_EXAMPLE_YAML = Path(__file__).parent / "testdata" / "portfolio.example.yaml"


def test_portfolio_yaml_cost_basis_flows_into_sp500_sale_realized_gain() -> None:
    """End-to-end check that an explicit cost basis on the portfolio statement lands on
    the SP500 tax lot and shapes the realized gain (and the resulting tax accrual) of a
    sale at simulation time. The example YAML carries `wealthfront_sp500` with
    `market_value_usd: 50_000` and `cost_basis.amount_usd: 30_000`; selling $20k with
    proportional basis allocation realizes a $20k × ($30k / $50k) = $12k basis and an
    $8k gain. If the YAML basis were dropped the simulator would treat the basis as
    either $0 (gain = $20k) or $50k (gain = $0) — neither matches the bracket math
    below."""
    portfolio = load_portfolio_yaml(_PORTFOLIO_EXAMPLE_YAML)
    initial_balance_sheet = portfolio.to_initial_balance_sheet()

    scenario = Scenario(
        scenario_id="portfolio_yaml_sp500_sale",
        label="Portfolio YAML SP500 Sale",
        actors=(Actor(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        # prior_year_tax_usd=0 opts out of quarterly estimated payments so the asserted
        # cash trajectory below sees only the sale-month tax accrual.
        tax_profile=TaxProfile(prior_year_tax_usd=0),
        initial_balance_sheet=initial_balance_sheet,
        policies=(
            # Cash floor sits above the YAML's $12,500 checking balance so the policy fires
            # once at month 0, raises the $20k tranche, and is satisfied for the rest of
            # the horizon.
            CheckingFloorSellPublicStockPolicy(
                policy_id="raise_cash", actor_id="owner", floor_usd=25_000, sale_amount_usd=20_000
            ),
        ),
    )

    result = _run_scenario(scenario, horizon_months=1)
    rollout = result.rollout(0)

    # Proportional basis on a $20k slice of a $50k position with $30k basis.
    expected_basis = 20_000 * 30_000 / 50_000
    expected_gain = 20_000 - expected_basis
    # Tax expense from the engine's bracket math on the $8k realized gain; if the YAML
    # basis ever stops flowing through, this number changes the moment the gain does.
    expected_tax = 22.94

    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_USD)[0], 20_000)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_BASIS_USD)[0], expected_basis)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_GAIN_USD)[0], expected_gain)
    assert_allclose(rollout.series(ReportMetric.GENERIC_SP500_SALE_TAX_USD)[0], expected_tax)

    sell_effects = result.effects(SellSp500Effect)
    assert len(sell_effects) == 1
    assert sell_effects[0].month_index == 0
    assert sell_effects[0].basis_usd == expected_basis
    assert sell_effects[0].gain_usd == expected_gain
    assert_allclose(sell_effects[0].tax_usd, expected_tax)


def test_portfolio_yaml_cost_basis_flows_into_private_equity_sale_realized_gain() -> None:
    """End-to-end check that an explicit cost basis on a PrivateEquityLot lands on the
    PE tax lot and shapes the realized gain of a tender sale. The example YAML carries
    `private_company_seed_lot` with `mark_value_usd: 25_000` and `cost_basis.amount_usd:
    5_000`; selling $10k with proportional basis allocation realizes a $10k × ($5k /
    $25k) = $2k basis and an $8k gain."""
    portfolio = load_portfolio_yaml(_PORTFOLIO_EXAMPLE_YAML)
    initial_balance_sheet = portfolio.to_initial_balance_sheet()

    scenario = Scenario(
        scenario_id="portfolio_yaml_pe_sale",
        label="Portfolio YAML PE Sale",
        actors=(Actor(actor_id="owner", label="Owner", role=ActorRole.PRIMARY_OWNER),),
        tax_profile=TaxProfile(prior_year_tax_usd=0),
        initial_balance_sheet=initial_balance_sheet,
        policies=(
            PrivateEquitySalePolicy(
                policy_id="tender_sale",
                actor_id="owner",
                proceeds_destination="cash",
                sale_rule=FixedAmountPrivateEquitySaleRule(amount_usd=10_000),
            ),
        ),
    )

    result = _run_scenario(
        scenario,
        horizon_months=2,
        market_provider=NoopMarketBundleProvider(private_equity_sale_opportunity_months=(1,)),
    )
    rollout = result.rollout(0)

    expected_basis = 10_000 * 5_000 / 25_000
    expected_gain = 10_000 - expected_basis
    expected_tax = 22.94

    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_USD)[1], 10_000)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD)[1], expected_basis)
    assert_allclose(rollout.series(ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD)[1], expected_tax)

    sell_effects = result.effects(SellPrivateEquityEffect)
    assert len(sell_effects) == 1
    assert sell_effects[0].basis_usd == expected_basis
    assert sell_effects[0].taxable_gain_usd == expected_gain
    assert_allclose(sell_effects[0].estimated_tax_usd, expected_tax)


def test_simulator_rejects_initial_position_without_explicit_cost_basis() -> None:
    """The engine seeds tax lots from `*Position.cost_basis_usd`; a `None` basis used to
    silently fall back to `value_usd` (zero gain on sale) which made every concentrated
    holding look tax-free. The scenario engine now refuses to start with a missing basis
    and points at the position so the caller knows what to fix."""
    scenario = Scenario(
        scenario_id="missing_basis",
        label="Missing Basis",
        actors=(_simple_actor(),),
        initial_balance_sheet=InitialBalanceSheet(
            assets=(
                GenericSp500StockPosition(
                    asset_id="sp500",
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    owner_actor_id="alpha",
                    value_usd=50_000,
                    # cost_basis_usd intentionally omitted.
                ),
            )
        ),
    )

    with pytest.raises(ValueError, match="GenericSp500StockPosition 'sp500' has no cost_basis_usd"):
        _run_scenario(scenario, horizon_months=1)


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


def _outside_rent_scenario(
    *,
    scenario_id: str,
    initial_cash_usd: float,
    monthly_rent_usd: float = 3_000,
    end_month: int | None = None,
    policies: tuple = (),
    sp500_value_usd: float = 0,
) -> Scenario:
    """A minimal `OWNER_RENTS_ELSEWHERE` scenario for the outside-rent obligation path.

    No property is selected — outside rent flows from the occupancy plan alone, not
    from a property the actor owns. SP500 is optional so the rescue-policy test can
    show a sale path.
    """
    assets: tuple = ()
    if sp500_value_usd > 0:
        assets = (
            GenericSp500StockPosition(
                asset_id="sp500", owner_actor_id="alpha", value_usd=sp500_value_usd, cost_basis_usd=sp500_value_usd
            ),
        )
    return Scenario(
        scenario_id=scenario_id,
        label=scenario_id.replace("_", " ").title(),
        actors=(_simple_actor(),),
        occupancy_plan=OccupancyPlan(
            occupancy_mode=OccupancyMode.OWNER_RENTS_ELSEWHERE,
            outside_rent_monthly_usd=monthly_rent_usd,
            end_month=end_month,
        ),
        policies=policies,
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking",
                    account_type=AccountType.CHECKING,
                    owner_actor_id="alpha",
                    balance_usd=initial_cash_usd,
                ),
            ),
            assets=assets,
        ),
    )


def test_outside_rent_obligation_settles_when_cash_available() -> None:
    """Happy path: `OWNER_RENTS_ELSEWHERE` + $3000/mo rent over 12 months emits one
    PAID `OUTSIDE_RENT` obligation per month and the cash trajectory dips by $3000
    each month."""
    horizon_months = 12
    scenario = _outside_rent_scenario(scenario_id="outside_rent_paid", initial_cash_usd=100_000, monthly_rent_usd=3_000)
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    outside_rent_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.OUTSIDE_RENT
    )
    # Rent accrues every month from 0 through horizon (inclusive of the snapshot
    # month) — the engine treats month_index values 0..horizon as 13 columns and
    # the occupancy span spans the full horizon by default.
    assert len(outside_rent_obligations) == horizon_months + 1
    assert {obligation.status for obligation in outside_rent_obligations} == {ObligationStatus.PAID}
    assert {obligation.amount_due_usd for obligation in outside_rent_obligations} == {3_000}
    # Settlement journal entries credit cash for $3000 every month.
    assert_allclose(
        _posting_matrix(
            result,
            role=ChartAccountRole.OUTSIDE_RENT_EXPENSE,
            side=PostingSide.DEBIT,
            journal_entry_type=JournalEntryType.OBLIGATION_SETTLEMENT,
        ),
        np.full((1, horizon_months + 1), 3_000.0, dtype="float64"),
    )
    # Cash trajectory: $100k starting, $3000 settles at every snapshot month
    # (including month 0 — rent is due upfront for the snapshot period, unlike
    # property carrying costs which exclude month 0).
    cash_series = result.rollout(0).series(ReportMetric.CASH_USD)
    expected_cash = [100_000 - 3_000 * (month + 1) for month in range(horizon_months + 1)]
    assert_allclose(cash_series, expected_cash)
    # The new enum variant carries a creditor and a unique obligation_id per month.
    creditors = {decision.creditor_id for decision in outside_rent_obligations}
    assert creditors == {"landlord"}


def test_outside_rent_obligation_fails_rollout_when_unfundable() -> None:
    """Cash-strapped renter with no rescue policy: the first month's OUTSIDE_RENT
    obligation flips the rollout to FAILED and emits a FailureEvent keyed to
    OUTSIDE_RENT."""
    scenario = _outside_rent_scenario(
        scenario_id="outside_rent_unfundable", initial_cash_usd=500, monthly_rent_usd=3_000
    )
    horizon_months = 4
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.FAILED
    rent_failures = tuple(
        event for event in result.arrays.failure_events if event.obligation_id.startswith("outside_rent")
    )
    # Every rent month after cash dries up emits a failure event. With $500 cash
    # and $3000 monthly rent, the very first month is already a shortfall.
    assert len(rent_failures) == horizon_months + 1
    assert rent_failures[0].month_index == 0
    assert all(event.unpaid_amount_usd > 0 for event in rent_failures)
    unfunded = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("outside_rent") and decision.decision_type is FundingDecisionType.UNFUNDED
    )
    assert len(unfunded) == horizon_months + 1
    assert all(decision.shortfall_usd > 0 for decision in unfunded)


def test_outside_rent_shortfall_can_be_rescued_by_checking_floor_sale_policy() -> None:
    """A `CheckingFloorSellPublicStockPolicy` rescues an outside-rent shortfall by
    selling SP500 stock to make rent. The rollout stays ACTIVE; the funding decision
    is `SELL_PUBLIC_STOCK` against the OUTSIDE_RENT obligation."""
    scenario = _outside_rent_scenario(
        scenario_id="outside_rent_rescued",
        initial_cash_usd=500,
        monthly_rent_usd=3_000,
        sp500_value_usd=100_000,
        policies=(
            # `sale_amount_usd` must clear the full $3000 rent in a single sale —
            # partial covers still leave a shortfall on a required obligation,
            # which the engine treats as FAILED.
            CheckingFloorSellPublicStockPolicy(
                policy_id="rent_funding_sale", actor_id="alpha", floor_usd=0, sale_amount_usd=5_000
            ),
        ),
    )
    horizon_months = 3
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    rent_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.OUTSIDE_RENT
    )
    assert len(rent_obligations) == horizon_months + 1
    assert {obligation.status for obligation in rent_obligations} == {ObligationStatus.PAID}
    assert tuple(event for event in result.arrays.failure_events) == ()
    sale_funding = tuple(
        decision
        for decision in result.arrays.funding_decisions
        if decision.obligation_id.startswith("outside_rent")
        and decision.decision_type is FundingDecisionType.SELL_PUBLIC_STOCK
    )
    assert len(sale_funding) >= 1
    assert {decision.policy_id for decision in sale_funding} == {"rent_funding_sale"}
    assert all(decision.funded_cash_usd > 0 for decision in sale_funding)


def test_outside_rent_stops_when_occupancy_span_ends() -> None:
    """Occupancy span ending mid-rollout (end_month=5) stops rent accrual at month
    6 onward; only months 0-5 produce OUTSIDE_RENT obligations."""
    scenario = _outside_rent_scenario(
        scenario_id="outside_rent_span_ends", initial_cash_usd=100_000, monthly_rent_usd=3_000, end_month=5
    )
    horizon_months = 12
    result = _run_scenario(scenario, horizon_months=horizon_months)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    rent_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.OUTSIDE_RENT
    )
    # Months 0..5 inclusive = 6 obligations.
    assert len(rent_obligations) == 6
    assert {obligation.month_index for obligation in rent_obligations} == {0, 1, 2, 3, 4, 5}
    assert {obligation.status for obligation in rent_obligations} == {ObligationStatus.PAID}
    # Cash stops dipping once rent stops: months 6..12 hold steady at the post-rent balance.
    cash_series = result.rollout(0).series(ReportMetric.CASH_USD)
    expected_post_rent_cash = 100_000 - 3_000 * 6
    assert_allclose(cash_series[6:], expected_post_rent_cash)


def test_outside_rent_zero_amount_produces_no_obligations() -> None:
    """`outside_rent_monthly_usd=0` skips the accrual entirely — no obligations, no
    settlements. Zero-amount obligations would be noise on the trace."""
    scenario = _outside_rent_scenario(scenario_id="outside_rent_zero", initial_cash_usd=10_000, monthly_rent_usd=0)
    result = _run_scenario(scenario, horizon_months=6)
    assert result.arrays is not None
    assert result.rollout(0).status().status == RolloutStatusType.ACTIVE
    outside_rent_obligations = tuple(
        obligation
        for obligation in result.arrays.obligations
        if obligation.obligation_type is ObligationType.OUTSIDE_RENT
    )
    assert outside_rent_obligations == ()
    # Cash is unchanged because no obligation accrues.
    assert_allclose(result.rollout(0).series(ReportMetric.CASH_USD), 10_000)


def test_two_pe_issuers_emit_independent_sale_opportunity_observations() -> None:
    """A scenario with two PE issuers (both riding the `"default"` market path) emits
    one `PrivateEquitySaleOpportunityObservation` per (rollout, month, issuer) at every
    tender month. Aggregate cash and tax flows are unchanged from the single-issuer
    case — the per-issuer split is purely an observation-emission concern in this slice.
    """
    scenario = Scenario(
        scenario_id="two_pe_issuers",
        label="Two PE Issuers",
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
                    asset_id="pe_a",
                    owner_actor_id="alpha",
                    issuer_id="issuer_a",
                    value_usd=120_000,
                    cost_basis_usd=120_000,
                    units=100,
                ),
                PrivateEquityPosition(
                    asset_id="pe_b",
                    owner_actor_id="alpha",
                    issuer_id="issuer_b",
                    value_usd=80_000,
                    cost_basis_usd=80_000,
                    units=80,
                ),
            ),
        ),
    )
    result = _run_scenario(
        scenario,
        horizon_months=6,
        market_provider=NoopMarketBundleProvider(private_equity_sale_opportunity_months=(3, 6)),
    )

    observations = result.rollout(0).market_observations(PrivateEquitySaleOpportunityObservation)
    by_month = {month: [obs for obs in observations if obs.month_index == month] for month in (3, 6)}
    assert {obs.source_asset_id for obs in by_month[3]} == {"issuer_a", "issuer_b"}
    assert {obs.source_asset_id for obs in by_month[6]} == {"issuer_a", "issuer_b"}
    # Both issuers ride the "default" flat path (multiplier=1.0), so per-issuer
    # values equal each issuer's initial mark.
    by_issuer_month_3 = {obs.source_asset_id: obs for obs in by_month[3]}
    assert_allclose(by_issuer_month_3["issuer_a"].private_equity_value_before_sale_usd, 120_000)
    assert_allclose(by_issuer_month_3["issuer_b"].private_equity_value_before_sale_usd, 80_000)
    # Aggregate cash trajectory is unchanged from no-sale baseline (no sale policy).
    assert_allclose(result.rollout(0).series(ReportMetric.CASH_USD), 10_000)
    # The aggregated PE value series equals the sum of the two initial marks at every month.
    assert_allclose(result.rollout(0).series(ReportMetric.PRIVATE_EQUITY_VALUE_USD), 200_000)


def test_per_issuer_value_multipliers_route_independently() -> None:
    """When the bundle carries distinct per-issuer multiplier paths, each issuer's
    observation `private_equity_value_before_sale_usd` reflects that issuer's
    multiplier — not the global default. This exercises the dict-keyed lookup
    end-to-end (provider → bundle → engine → observation)."""
    scenario = Scenario(
        scenario_id="per_issuer_routing",
        label="Per Issuer Routing",
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
                    asset_id="pe_a",
                    owner_actor_id="alpha",
                    issuer_id="issuer_a",
                    value_usd=100_000,
                    cost_basis_usd=100_000,
                    units=100,
                ),
                PrivateEquityPosition(
                    asset_id="pe_b",
                    owner_actor_id="alpha",
                    issuer_id="issuer_b",
                    value_usd=100_000,
                    cost_basis_usd=100_000,
                    units=100,
                ),
            ),
        ),
    )
    # issuer_a grows 2× by month 6; issuer_b stays flat.
    provider = NoopMarketBundleProvider(
        private_equity_sale_opportunity_months=(6,),
        private_equity_value_paths_by_issuer={
            "issuer_a": (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0),
            "issuer_b": (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        },
        private_equity_sale_opportunity_months_by_issuer={"issuer_a": (6,), "issuer_b": (6,)},
    )
    result = _run_scenario(scenario, horizon_months=6, market_provider=provider)
    observations = [
        obs
        for obs in result.rollout(0).market_observations(PrivateEquitySaleOpportunityObservation)
        if obs.month_index == 6
    ]
    by_issuer = {obs.source_asset_id: obs for obs in observations}
    assert set(by_issuer) == {"issuer_a", "issuer_b"}
    # issuer_a doubled; issuer_b is flat.
    assert_allclose(by_issuer["issuer_a"].private_equity_value_before_sale_usd, 200_000)
    assert_allclose(by_issuer["issuer_b"].private_equity_value_before_sale_usd, 100_000)


def test_two_crypto_symbols_route_to_per_symbol_paths() -> None:
    """Two crypto positions with distinct symbols (BTC + ETH) and a per-symbol
    multiplier dict route each to its own path. With aggregated crypto state in
    this slice the engine still tracks one cash trajectory; the routing surfaces
    through per-symbol path lookup verified via the bundle directly. The cash
    flow stays consistent with the legacy single-asset behavior — no policy
    fires here, so cash is unchanged.
    """
    scenario = Scenario(
        scenario_id="two_crypto_symbols",
        label="Two Crypto Symbols",
        actors=(_simple_actor(),),
        tax_profile=TaxProfile(),
        initial_balance_sheet=InitialBalanceSheet(
            accounts=(
                AccountBalance(
                    account_id="checking", account_type=AccountType.CHECKING, owner_actor_id="alpha", balance_usd=10_000
                ),
            ),
            assets=(
                CryptoAssetPosition(
                    asset_id="btc_holding",
                    owner_actor_id="alpha",
                    value_usd=5_000,
                    cost_basis_usd=5_000,
                    asset_symbol="BTC",
                    quantity=0.1,
                ),
                CryptoAssetPosition(
                    asset_id="eth_holding",
                    owner_actor_id="alpha",
                    value_usd=3_000,
                    cost_basis_usd=3_000,
                    asset_symbol="ETH",
                    quantity=2.0,
                ),
            ),
        ),
    )
    provider = NoopMarketBundleProvider(crypto_value_paths_by_symbol={"BTC": (1.0, 1.0, 1.5), "ETH": (1.0, 1.0, 0.5)})
    # Verify the provider populates per-symbol routing dicts on the bundle. The
    # `required_keys` declaration mirrors what `simulate_set` would extract from
    # the scenario above.
    bundle = provider.sample_market_bundle(
        rollout_count=2,
        horizon_months=2,
        seed=0,
        market_request=MarketRequest(market_model_id="t", rollout_count=2, horizon_months=2, seed=0),
        required_keys=RequiredMarketKeys(crypto_symbols=frozenset({"BTC", "ETH"})),
    )
    assert set(bundle.crypto_value_multipliers_by_symbol) == {"BTC", "ETH"}
    assert_allclose(bundle.crypto_value_multiplier("BTC")[:, 2], 1.5)
    assert_allclose(bundle.crypto_value_multiplier("ETH")[:, 2], 0.5)
    # An unknown symbol now raises — there is no `"default"` fallback path.
    with pytest.raises(MissingMarketFactorError):
        bundle.crypto_value_multiplier("XRP")

    # End-to-end run still succeeds; cash is unchanged with no sale policy in play.
    result = _run_scenario(scenario, horizon_months=2, market_provider=provider)
    assert_allclose(result.rollout(0).series(ReportMetric.CASH_USD), 10_000)


if __name__ == "__main__":
    pytest_bazel.main()
