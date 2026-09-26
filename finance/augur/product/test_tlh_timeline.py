"""Real component settlement survives dense capture and the product timeline.

A flat index and a 12% annual reduced-form loss yield give the $100 opening
cohort one $1 modeled loss at month zero. These are trace/accounting controls, not a forecast.
"""

from decimal import Decimal
from typing import Literal

import pytest
import pytest_bazel

from finance.augur.product.action_projection import metric_arrays
from finance.augur.product.projection import project_product_rollout
from finance.augur.product.wire import HoldingSaleEvent, TlhFinancialEffectEvent
from finance.augur.sim.actions import Contribute, DecisionActions, Liquidate, Withdraw
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.events import TlhOperation
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, PortfolioId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedJurisdiction, PreparedSeries, PreparedTlhPortfolio
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import ORDINARY_INCOME, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort
from finance.augur.sim.world import World

ASSET = AssetId("test-managed-index")
# Money is counted in whole dollars here, so the stipulated $1 price is one quantum.
QUANTUM = Decimal(1)
FEDERAL = load_jurisdiction(JurisdictionId("federal_us"))


def compose(price: int) -> World:
    """The owner's $10 and a managed cohort of 100 index units bought for $100 two years ago, at `price` throughout."""
    world = World(
        MarketPath(
            (PreparedSeries(series_id=f"security:{ASSET}", snapshots=2, values=(price, price)),), 0, rollout_count=1
        ),
        horizon_months=1,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=JurisdictionId("federal_us"), level=FEDERAL.level),),
    )
    for agent_id, balance in ((AgentId("owner"), 10), (AgentId("irs"), 0)):
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=agent_id, account_id=AccountId("checking")), opening_balance=balance
            )
        )
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id="owner", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs"),
                {JurisdictionId("federal_us"): FEDERAL},
                quantum=QUANTUM,
            )
        )
    )
    world.declare_portfolio(
        PreparedTlhPortfolio(
            portfolio_id=PortfolioId("managed"),
            owner_agent_id=AgentId("owner"),
            account_id=AccountId("checking"),
            asset_id=ASSET,
            initial_cohorts=(TlhOpeningCohort(value=100 * price, cost_basis=100, purchase_month_index=-24),),
            assumptions=TlhAssumptions(
                peak_annual_yield=0.12,
                floor_annual_yield=0,
                maturity_decay_exponent=1,
                drawdown_sensitivity=0,
                short_term_fraction=1,
            ),
        )
    )
    return world


@pytest.mark.parametrize("capture", ["dense", "forensic"])
def test_tlh_cash_and_separate_realizations_reach_product_timeline(capture: Literal["dense", "forensic"]) -> None:
    session = ActionSession({0: compose(price=1)}, AgentId("owner"), capture=capture)
    try:
        assert not isinstance(session.start(), Finished)
        result = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Contribute(
                            cause_id="deposit",
                            agent_id=AgentId("owner"),
                            portfolio_id=PortfolioId("managed"),
                            cash_account_id=AccountId("checking"),
                            amount=10,
                        ),
                        Withdraw(
                            cause_id="withdraw",
                            agent_id=AgentId("owner"),
                            portfolio_id=PortfolioId("managed"),
                            cash_account_id=AccountId("checking"),
                            amount=100,
                        ),
                    ],
                )
            ]
        )
    finally:
        session.close()
    assert isinstance(result, Finished)
    [rollout] = result.rollouts
    assert rollout.stop is None
    assert rollout.trace is not None
    events = rollout.trace.events
    assert events.lot_dispositions.is_empty()
    assert events.tlh_financial_effects.select(
        "operation", "cash_amount_quanta", "short_term_gain_quanta", "long_term_gain_quanta", "basis_change_quanta"
    ).rows() == [
        (TlhOperation.MODELED_REALIZATION, 0, -1, 0, -1),
        (TlhOperation.CONTRIBUTION, -10, 0, 0, 10),
        (TlhOperation.REDEMPTION, 100, 0, 1, -99),
    ]
    projected = project_product_rollout(
        events,
        metric_arrays(
            result.rollouts,
            primary_agent_id=AgentId("owner"),
            horizon_months=1,
            currency_code="USD",
            currency_quantum="1",
        ),
        rollout_id=0,
        primary_agent_id=AgentId("owner"),
        asset_label_by_id={},
    )
    assert not any(isinstance(event, HoldingSaleEvent) for event in projected.events)
    effects = [event for event in projected.events if isinstance(event, TlhFinancialEffectEvent)]
    assert [(event.operation, event.amount_quanta, event.cause_id) for event in effects] == [
        (TlhOperation.MODELED_REALIZATION, "0", "tlh:managed:advance:m0"),
        (TlhOperation.CONTRIBUTION, "-10", "deposit"),
        (TlhOperation.REDEMPTION, "100", "withdraw"),
    ]
    assert [(event.short_term_gain_quanta, event.long_term_gain_quanta) for event in effects] == [
        ("-1", "0"),
        ("0", "0"),
        ("0", "1"),
    ]
    assert all(event.portfolio_id == "managed" and event.month_index == 0 for event in effects)
    assert [event.cash_account_id for event in effects] == [None, "checking", "checking"]
    assert projected.monthly_metric_arrays["cash_quanta"].tolist() == [10, 100]
    assert projected.monthly_metric_arrays["holding_value_quanta"].tolist() == [100, 10]
    [gain] = rollout.summary.ending_book.capital_gains
    assert (gain.short_term_gain, gain.long_term_gain) == (-1, 1)
    assert bool(rollout.trace.journal) == (capture == "forensic")


def test_zero_cash_liquidation_is_still_a_redemption() -> None:
    # A statement at a zero mark reports every cohort at zero value, its basis intact.
    session = ActionSession({0: compose(price=0)}, AgentId("owner"), capture="dense")
    try:
        assert not isinstance(session.start(), Finished)
        result = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Liquidate(
                            cause_id="close-worthless",
                            agent_id=AgentId("owner"),
                            portfolio_id=PortfolioId("managed"),
                            cash_account_id=AccountId("checking"),
                        )
                    ],
                )
            ]
        )
    finally:
        session.close()
    assert isinstance(result, Finished)
    [rollout] = result.rollouts
    assert rollout.stop is None
    assert rollout.trace is not None
    redemption = rollout.trace.events.tlh_financial_effects.filter(operation=TlhOperation.REDEMPTION)
    assert redemption.select(
        "cash_amount_quanta", "short_term_gain_quanta", "long_term_gain_quanta", "basis_change_quanta"
    ).rows() == [(0, 0, -100, -100)]


if __name__ == "__main__":
    pytest_bazel.main()
