"""Real component settlement survives dense capture and the product timeline.

Stipulated flat $1 units and a 12% annual reduced-form loss yield give one $1
modeled loss at month zero. These are trace/accounting controls, not a forecast.
"""

from decimal import Decimal
from typing import Literal

import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.action_projection import metric_arrays
from finance.augur.product.projection import project_product_rollout
from finance.augur.product.wire import HoldingSaleEvent, TlhFinancialEffectEvent
from finance.augur.sim.actions import Contribute, DecisionActions, Liquidate, Withdraw
from finance.augur.sim.events import TlhOperation
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import Currency, InitialLot, TaxProfile, TlhPortfolioSpec
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking
from finance.augur.sim.tlh import TlhAssumptions


@pytest.fixture
def case() -> Case:
    asset = SecurityKey(symbol=SecuritySymbol("test-managed-index"))
    return Case(
        scenario=scenario(
            checking(("owner", Decimal(10)), ("irs", Decimal(0))),
            tax_profiles=[TaxProfile(agent_id="owner", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
            horizon_months=1,
            currency=Currency(quantum=Decimal(1)),
            tlh_portfolios=[
                TlhPortfolioSpec(
                    portfolio_id="managed",
                    owner_agent_id="owner",
                    account_id="checking",
                    asset=asset,
                    initial_lots=[
                        InitialLot(
                            lot_id="imported",
                            agent_id="owner",
                            account_id="checking",
                            asset=asset,
                            purchase_month_index=-24,
                            quantity=100,
                            cost_basis=Decimal(100),
                        )
                    ],
                    assumptions=TlhAssumptions(
                        peak_annual_yield=0.12,
                        floor_annual_yield=0,
                        maturity_decay_exponent=1,
                        drawdown_sensitivity=0,
                        short_term_fraction=1,
                    ),
                )
            ],
        ),
        rollout_count=1,
        series={asset: levels([[Decimal(1), Decimal(1)]])},
    )


@pytest.mark.parametrize("capture", ["dense", "forensic"])
def test_tlh_cash_and_separate_realizations_reach_product_timeline(
    case: Case, capture: Literal["dense", "forensic"]
) -> None:
    session = ActionSession(case.compiled_run, "owner", [0], capture=capture)
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
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
                            amount=10,
                        ),
                        Withdraw(
                            cause_id="withdraw",
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
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
        metric_arrays(case.compiled_run, result.rollouts, primary_agent_id="owner"),
        rollout_id=0,
        primary_agent_id="owner",
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


def test_zero_cash_liquidation_is_still_a_redemption(case: Case) -> None:
    worthless = Case(
        scenario=case.scenario,
        rollout_count=1,
        series={asset: levels([[Decimal(0), Decimal(0)]]) for asset in case.series},
    )
    session = ActionSession(worthless.compiled_run, "owner", [0], capture="dense")
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
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
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
