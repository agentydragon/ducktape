"""The real session posts opaque component effects without owning its model state."""

from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.books import TlhPortfolioState
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction
from finance.augur.sim.scenario import InitialLot, TlhPortfolioSpec
from finance.augur.sim.session import Action, ActionSession, DecisionActions
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking
from finance.augur.sim.tlh import TlhAssumptions, TlhPortfolio

ASSET = SecurityKey(symbol=SecuritySymbol("managed-index"))


def _case(*, cash: int = 0, horizon: int = 2, rollouts: int = 1, harvest: bool = True) -> Case:
    return Case(
        scenario=scenario(
            checking(("owner", Decimal(cash))),
            horizon_months=horizon,
            currency_quantum="1",
            tlh_portfolios=[
                TlhPortfolioSpec(
                    portfolio_id="managed",
                    owner_agent_id="owner",
                    account_id="checking",
                    asset=ASSET,
                    initial_lots=[
                        InitialLot(
                            lot_id="imported",
                            agent_id="owner",
                            account_id="checking",
                            asset=ASSET,
                            purchase_month_index=-24,
                            quantity=100.0,
                            cost_basis=Decimal(100),
                        )
                    ],
                    assumptions=TlhAssumptions(
                        peak_annual_yield=0.12 if harvest else 0,
                        floor_annual_yield=0,
                        maturity_decay_exponent=1,
                        drawdown_sensitivity=0,
                        short_term_fraction=1,
                    ),
                )
            ],
        ),
        rollout_count=rollouts,
        series={ASSET: levels([[Decimal(1)] * (horizon + 1)] * rollouts)},
    )


def test_managed_opening_is_not_an_ordinary_lot_and_sale_follows_same_month_loss() -> None:
    case = _case(horizon=1)
    assert case.compiled_run.scenario.initial_lots == ()
    session = ActionSession(case.compiled_run, "owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        [decision] = batch
        assert decision.observation.public_positions == []
        [portfolio] = decision.observation.tlh_portfolios
        assert (portfolio.value, portfolio.reported_tax_basis) == (100, 99)
        result = session.advance([DecisionActions(0, 0, [Action.liquidate("sell", "owner", "managed", "checking")])])
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.stop is None
    assert rollout.trace is not None
    assert rollout.trace.books[0].tlh_portfolios[0].reported_tax_basis == 100
    assert rollout.summary.ending_book.tlh_portfolios == [
        TlhPortfolioState(
            portfolio_id="managed",
            owner_agent_id="owner",
            account_id="checking",
            asset_id="managed-index",
            value=0,
            reported_tax_basis=0,
        )
    ]
    assert rollout.summary.cash[0].values[-1] == 100
    # Modeled ST loss and actual LT gain are separately characterized, never a cash subsidy.
    [gain] = rollout.summary.ending_book.capital_gains
    assert (gain.short_term_gain, gain.long_term_gain) == (-1, 1)
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in rollout.trace.journal)


def test_rejected_contribution_preserves_harvest_and_earlier_withdrawal_without_future_policy_calls() -> None:
    session = ActionSession(_case(rollouts=2).compiled_run, "owner", [0, 1])
    try:
        first = session.start()
        assert not isinstance(first, Finished)
        second = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Action.withdraw("cash", "owner", "managed", "checking", 10),
                        Action.contribute("too-much", "owner", "managed", "checking", 11),
                        Action.liquidate("never", "owner", "managed", "checking"),
                    ],
                ),
                DecisionActions(1, 0, []),
            ]
        )
        assert not isinstance(second, Finished)
        assert [(row.rollout_id, row.observation.month) for row in second] == [(1, 1)]
        result = session.advance([DecisionActions(1, 1, [])])
        assert isinstance(result, Finished)
    finally:
        session.close()
    stopped, live = result.rollouts
    assert stopped.stop == RejectedAction(month=0, action_index=1)
    assert live.stop is None
    assert stopped.trace is not None
    assert len(stopped.trace.books) == 2
    assert [type(receipt.outcome) for receipt in stopped.trace.receipts] == [Executed, Rejected]
    [portfolio] = stopped.summary.ending_book.tlh_portfolios
    assert (portfolio.value, portfolio.reported_tax_basis) == (90, 89)
    assert stopped.summary.cash[0].values == [0, 10]
    assert stopped.summary.ending_book.capital_gains[0].short_term_gain == -1


@pytest.mark.parametrize("amount", [-1, 101])
def test_invalid_withdrawal_changes_no_component_state(amount: int) -> None:
    session = ActionSession(_case(horizon=1, harvest=False).compiled_run, "owner", [0])
    try:
        session.start()
        result = session.advance(
            [DecisionActions(0, 0, [Action.withdraw("invalid", "owner", "managed", "checking", amount)])]
        )
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.stop == RejectedAction(month=0, action_index=0)
    [portfolio] = rollout.summary.ending_book.tlh_portfolios
    assert (portfolio.value, portfolio.reported_tax_basis) == (100, 100)
    assert rollout.summary.cash[0].values[-1] == 0


def test_another_actors_component_is_neither_observed_nor_redeemable() -> None:
    case = _case(horizon=1, harvest=False)
    case = replace(
        case,
        scenario=scenario(
            checking(("owner", Decimal(0)), ("other", Decimal(0))),
            horizon_months=1,
            currency_quantum="1",
            tlh_portfolios=case.scenario.tlh_portfolios,
        ),
    )
    session = ActionSession(case.compiled_run, "other", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        assert batch[0].observation.tlh_portfolios == []
        result = session.advance([DecisionActions(0, 0, [Action.liquidate("steal", "owner", "managed", "checking")])])
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.stop == RejectedAction(month=0, action_index=0)
    assert rollout.summary.ending_book.tlh_portfolios[0].value == 100


def test_model_defect_closes_session_instead_of_becoming_a_rejected_action(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ActionSession(_case().compiled_run, "owner", [0])

    def broken_advance(self, market):
        raise ArithmeticError("model defect")

    monkeypatch.setattr(TlhPortfolio, "advance", broken_advance)
    with pytest.raises(ArithmeticError, match="model defect"):
        session.start()
    with pytest.raises(ValueError, match=r"closed|consumed|finished"):
        session.advance([DecisionActions(0, 0, [])])
    session.close()


if __name__ == "__main__":
    pytest_bazel.main()
