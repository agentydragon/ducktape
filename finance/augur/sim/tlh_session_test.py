"""The real session posts opaque component effects without owning its model state."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.product.action_projection import metric_arrays
from finance.augur.sim.actions import Action, Contribute, DecisionActions, Liquidate, Withdraw
from finance.augur.sim.books import TlhPortfolioState
from finance.augur.sim.configured import simulate_dense_json
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction
from finance.augur.sim.scenario import (
    Currency,
    DistributionTaxSlice,
    InitialLot,
    Scenario,
    SecurityDistribution,
    TaxProfile,
    TlhPortfolioSpec,
)
from finance.augur.sim.session import ActionSession, Capture
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking
from finance.augur.sim.tlh import TlhAssumptions, TlhMarketUpdate, TlhPortfolio

ASSET = SecurityKey(symbol=SecuritySymbol("managed-index"))


def _case(*, cash: int = 0, horizon: int = 2, rollouts: int = 1, harvest: bool = True) -> Case:
    return Case(
        scenario=scenario(
            checking(("owner", Decimal(cash)), ("irs", Decimal(0))),
            tax_profiles=[TaxProfile(agent_id="owner", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
            horizon_months=horizon,
            currency=Currency(quantum=Decimal(1)),
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
    assert case.compiled_run.currency_quantum == "1"
    assert case.compiled_run.scenario.initial_lots == ()
    session = ActionSession(case.compiled_run, "owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        [decision] = batch
        assert decision.observation.public_positions == ()
        [portfolio] = decision.observation.tlh_portfolios
        assert (portfolio.value, portfolio.reported_tax_basis) == (100, 99)
        result = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [Liquidate(cause_id="sell", agent_id="owner", portfolio_id="managed", cash_account_id="checking")],
                )
            ]
        )
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
                        Withdraw(
                            cause_id="cash",
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
                            amount=10,
                        ),
                        Contribute(
                            cause_id="too-much",
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
                            amount=11,
                        ),
                        Liquidate(
                            cause_id="never", agent_id="owner", portfolio_id="managed", cash_account_id="checking"
                        ),
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
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Withdraw(
                            cause_id="invalid",
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
                            amount=amount,
                        )
                    ],
                )
            ]
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
            tax_profiles=[],
            horizon_months=1,
            currency=Currency(quantum=Decimal(1)),
            tlh_portfolios=case.scenario.tlh_portfolios,
        ),
    )
    session = ActionSession(case.compiled_run, "other", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        assert batch[0].observation.tlh_portfolios == ()
        result = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [Liquidate(cause_id="steal", agent_id="owner", portfolio_id="managed", cash_account_id="checking")],
                )
            ]
        )
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.stop == RejectedAction(month=0, action_index=0)
    assert rollout.summary.ending_book.tlh_portfolios[0].value == 100


def test_model_defect_closes_session_instead_of_becoming_a_rejected_action(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ActionSession(_case().compiled_run, "owner", [0])

    def broken_advance(self: TlhPortfolio, market: TlhMarketUpdate) -> None:
        raise ArithmeticError("model defect")

    monkeypatch.setattr(TlhPortfolio, "advance", broken_advance)
    with pytest.raises(ArithmeticError, match="model defect"):
        session.start()
    with pytest.raises(ValueError, match=r"closed|consumed|finished"):
        session.advance([DecisionActions(0, 0, [])])
    session.close()


@pytest.mark.parametrize("capture", ["summary", "dense", "forensic"])
@pytest.mark.parametrize("reject", [False, True])
def test_closing_marks_and_product_projection_do_not_advance_the_model_early(capture: Capture, reject: bool) -> None:
    case = _case(horizon=1)
    case = replace(case, series={ASSET: levels([[Decimal(1), Decimal(2)]])})
    session = ActionSession(case.compiled_run, "owner", [0], capture=capture)
    try:
        session.start()
        actions: list[Action] = (
            [
                Withdraw(
                    cause_id="unfundable",
                    agent_id="owner",
                    portfolio_id="managed",
                    cash_account_id="checking",
                    amount=101,
                )
            ]
            if reject
            else []
        )
        result = session.advance([DecisionActions(0, 0, actions)])
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    [portfolio] = rollout.summary.ending_book.tlh_portfolios
    assert portfolio.value == (100 if reject else 200)
    assert portfolio.reported_tax_basis == 99
    assert rollout.summary.ending_book.capital_gains[0].short_term_gain == -1
    metrics = metric_arrays(case.compiled_run, result.rollouts, primary_agent_id="owner")
    assert metrics.base_series[1][:, 0].tolist() == [100, 100 if reject else 200]


def test_managed_subquantum_distribution_keeps_cash_and_issuer_character() -> None:
    case = _case(horizon=1, harvest=False)
    case = replace(
        case,
        scenario=case.scenario.model_copy(
            update={
                "security_distributions": [
                    SecurityDistribution(
                        agent_id="owner",
                        holding_account_id="checking",
                        asset=ASSET,
                        to_account_id="checking",
                        tax_character=(
                            DistributionTaxSlice(fraction=0.5, issuer_jurisdiction_id="federal_us"),
                            DistributionTaxSlice(fraction=0.5, issuer_jurisdiction_id=None),
                        ),
                    )
                ]
            }
        ),
        series={**case.series, SecurityDistributionKey(symbol=ASSET.symbol): levels([[Decimal("0.015"), Decimal(0)]])},
    )
    session = ActionSession(case.compiled_run, "owner", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        assert batch[0].observation.cash == 2  # round(100 × $0.015), then two $1 tax slices
        result = session.advance([DecisionActions(0, 0, [])])
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.trace is not None
    assert [(row.issuer_jurisdiction_id, row.units, row.amount) for row in rollout.trace.distributions] == [
        ("federal_us", None, 1),
        (None, None, 1),
    ]
    assert rollout.summary.ending_book.tlh_portfolios[0].value == 100
    assert {(row.income_source, row.income) for row in rollout.summary.ending_book.income} == {
        ("ordinary", 0),  # The income ledger retains every declared source, including zero buckets.
        ("interest:federal_us", 1),
        ("interest:corporate", 1),
    }


def test_contribution_is_first_harvested_in_the_next_month() -> None:
    session = ActionSession(_case(cash=100).compiled_run, "owner", [0])
    try:
        first = session.start()
        assert not isinstance(first, Finished)
        assert first[0].observation.tlh_portfolios[0].reported_tax_basis == 99
        second = session.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        Contribute(
                            cause_id="new",
                            agent_id="owner",
                            portfolio_id="managed",
                            cash_account_id="checking",
                            amount=100,
                        )
                    ],
                )
            ]
        )
        assert not isinstance(second, Finished)
        assert second[0].observation.tlh_portfolios[0].reported_tax_basis == 197
        result = session.advance([DecisionActions(0, 1, [])])
        assert isinstance(result, Finished)
    finally:
        session.close()
    [rollout] = result.rollouts
    assert rollout.trace is not None
    assert rollout.trace.books[1].tlh_portfolios[0].reported_tax_basis == 199
    assert rollout.summary.ending_book.capital_gains[0].short_term_gain == -3


def test_removed_or_misplaced_fields_cannot_silently_disable_the_model() -> None:
    case = _case()
    for name, value in (("harvest_policies", []), ("currency_quantum", "1")):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Scenario.model_validate({**case.scenario.model_dump(), name: value})
    [portfolio] = case.scenario.tlh_portfolios
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TlhPortfolioSpec.model_validate({**portfolio.model_dump(), "cumulative_harvest": 1})


def test_configured_clock_needs_no_placeholder_economic_actor() -> None:
    case = Case(scenario=Scenario(agents=[], initial_cash=[], tax_profiles=[], horizon_months=1), rollout_count=1)
    document = json.loads(simulate_dense_json(case.compiled_run))
    [rollout] = document["rollouts"]
    # The canonical ledger's external balancing account is not a configured decision actor.
    boundary = [{"account": {"agent_id": "__external__", "account_id": "boundary"}, "balance": 0}]
    assert [(book["month"], book["balances"]) for book in rollout["months"]] == [(0, boundary), (1, boundary)]


if __name__ == "__main__":
    pytest_bazel.main()
