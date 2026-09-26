"""Component financial facts settle atomically without recreating private holdings."""

from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.actions import Withdraw
from finance.augur.sim.books import EXTERNAL_BOUNDARY
from finance.augur.sim.holdings import gain_account
from finance.augur.sim.ids import AccountId, AssetId, JurisdictionId, PortfolioId
from finance.augur.sim.managed import ComponentEffects, InterestCredit, ManagedPortfolios, basis_account
from finance.augur.sim.money import MIN_COUNT
from finance.augur.sim.observations import TlhPortfolioObservation
from finance.augur.sim.prepared import PreparedSeries, PreparedTlhPortfolio
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, INCOME_SOURCES, accounting, world_on
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort
from finance.augur.sim.world import World

PRICES = PreparedSeries(series_id="security:test_fund", snapshots=3, values=(100, 110, 120) * 2)


@pytest.fixture
def spec() -> PreparedTlhPortfolio:
    return PreparedTlhPortfolio(
        portfolio_id=PortfolioId("managed"),
        owner_agent_id=HOUSEHOLD,
        account_id=AccountId("custody"),
        asset_id=AssetId("test_fund"),
        initial_cohorts=(TlhOpeningCohort(value=100, cost_basis=80, purchase_month_index=-1),),
        assumptions=TlhAssumptions(
            peak_annual_yield=0,
            floor_annual_yield=0,
            maturity_decay_exponent=1,
            drawdown_sensitivity=0,
            short_term_fraction=1,
        ),
    )


@pytest.fixture
def opening() -> TlhPortfolioObservation:
    return TlhPortfolioObservation(
        portfolio_id=PortfolioId("managed"),
        owner_agent_id=HOUSEHOLD,
        account_id=AccountId("custody"),
        asset_id=AssetId("test_fund"),
        value=100,
        reported_tax_basis=80,
        accepts_contributions=True,
    )


def composed(spec: PreparedTlhPortfolio, rollout_id: int) -> World:
    """The portfolio held on one of the two price paths."""
    world = world_on((PRICES,), horizon_months=2, rollout_id=rollout_id, rollout_count=2)
    world.declare_portfolio(spec)
    return world


@pytest.fixture
def world(spec: PreparedTlhPortfolio) -> World:
    return composed(spec, 0)


def fingerprint(world: World) -> tuple[object, ...]:
    return deepcopy(
        (
            dict(world.accounting.ledger.balances),
            world.accounting.tax.years,
            world.accounting.tax.income.by_source,
            world.accounting.journal,
            world.accounting.transfers,
            world.managed_portfolios().marks,
            world.managed_portfolios().effects,
            world.managed_portfolios().distributions,
            world.holdings.lots,
        )
    )


def harvest(opening: TlhPortfolioObservation) -> ComponentEffects:
    return ComponentEffects(opening.model_copy(update={"reported_tax_basis": 70}), None, 0, -10, 0)


def test_basis_statement_cash_and_tax_reconcile_without_ordinary_lots(
    world: World, opening: TlhPortfolioObservation
) -> None:
    world.managed_portfolios().settle(
        world.accounting, 0, HOUSEHOLD, "manager", harvest(opening), operation="modeled_realization"
    )
    contribution = ComponentEffects(
        opening.model_copy(update={"value": 120, "reported_tax_basis": 90}), AccountId("checking"), -20, 0, 0
    )
    world.managed_portfolios().settle(
        world.accounting, 0, HOUSEHOLD, "contribution", contribution, operation="contribution"
    )
    assert world.accounting.ledger.balance(CASH) == 80
    assert world.accounting.ledger.balance(basis_account(opening)) == 90
    assert not world.holdings.lots
    assert (
        world.accounting.tax.years[HOUSEHOLD].short_term_gain,
        world.accounting.tax.years[HOUSEHOLD].long_term_gain,
    ) == (-10, 0)
    assert world.accounting.ledger.trial_balance() == 0


@pytest.mark.parametrize("case", [0, 1, 2, 4, 5, 6, 7])
def test_invalid_effects_and_overflow_leave_every_financial_book_unchanged(
    world: World, opening: TlhPortfolioObservation, case: int
) -> None:
    effects = harvest(opening)
    if case == 0:
        effects = replace(effects, short_term_gain=-9)
    elif case == 1:
        effects = replace(effects, observation=effects.observation.model_copy(update={"owner_agent_id": "test_other"}))
    elif case == 2:
        effects = replace(effects, observation=effects.observation.model_copy(update={"reported_tax_basis": -1}))
    elif case == 4:
        world.accounting.tax.years[HOUSEHOLD].short_term_gain = MIN_COUNT
    elif case == 5:
        effects = ComponentEffects(
            opening.model_copy(update={"reported_tax_basis": 181}), AccountId("checking"), -101, 0, 0
        )
    else:
        amount = 10 if case == 6 else -10
        effects = ComponentEffects(
            opening,
            AccountId("checking"),
            amount,
            0,
            0,
            (InterestCredit(JurisdictionId("undeclared") if case == 6 else None, amount),),
        )
    before = fingerprint(world)
    with pytest.raises(
        (ValueError, OverflowError), match=r"reconcile|observation|overflow|available cash|declared income"
    ):
        world.managed_portfolios().settle(
            world.accounting, 0, HOUSEHOLD, "manager", effects, operation="modeled_realization"
        )
    assert fingerprint(world) == before


def test_distribution_cash_uses_interest_source_not_capital_gain_journal_account(
    world: World, opening: TlhPortfolioObservation
) -> None:
    effects = ComponentEffects(opening, AccountId("checking"), 5, 0, 0, (InterestCredit(None, 5),))
    world.managed_portfolios().settle(world.accounting, 0, HOUSEHOLD, "distribution", effects, operation="distribution")
    assert world.accounting.ledger.balance(CASH) == 105
    assert world.accounting.ledger.balance(gain_account(HOUSEHOLD)) == 0
    assert world.accounting.ledger.balance(EXTERNAL_BOUNDARY) == -5
    assert (
        world.accounting.tax.years[HOUSEHOLD].short_term_gain,
        world.accounting.tax.years[HOUSEHOLD].long_term_gain,
    ) == (0, 0)
    assert world.managed_portfolios().marks[PortfolioId("managed")].reported_tax_basis == 80


def test_withdrawal_receipt_does_not_recalculate_component_rounded_value(
    spec: PreparedTlhPortfolio, opening: TlhPortfolioObservation
) -> None:
    books = accounting()
    managed = ManagedPortfolios(INCOME_SOURCES, ())
    managed.open(books, spec, opening.model_copy(update={"value": 2, "reported_tax_basis": 2}))
    effects = ComponentEffects(
        opening.model_copy(update={"value": 0, "reported_tax_basis": 0}), AccountId("checking"), 1, 0, -1
    )
    action = Withdraw(
        cause_id="redemption",
        agent_id=HOUSEHOLD,
        portfolio_id=PortfolioId("managed"),
        cash_account_id=AccountId("checking"),
        amount=1,
    )
    managed.settle(books, 0, HOUSEHOLD, "redemption", effects, operation="redemption", action=action)
    assert managed.marks[PortfolioId("managed")].value == 0
    assert books.ledger.balance(CASH) == 101
    assert books.tax.years[HOUSEHOLD].long_term_gain == -1
    assert books.ledger.trial_balance() == 0


def test_component_marks_keep_explicit_stop_marks_and_independent_books(
    spec: PreparedTlhPortfolio, opening: TlhPortfolioObservation
) -> None:
    stopped, live = composed(spec, 1), composed(spec, 0)
    stopped.prepare_month(0, {}, {})
    stopped.assemble_claims([])
    stopped.managed_portfolios().mark([opening])
    stopped_values = [stopped.holding_value(HOUSEHOLD, stopped.mark_month)]
    stopped.close_books(failed=True, mortgages=[])
    stopped_values.append(stopped.holding_value(HOUSEHOLD, stopped.mark_month))
    live_values = [live.holding_value(HOUSEHOLD, live.mark_month)]
    for month in range(2):
        live.prepare_month(month, {}, {})
        live.assemble_claims([])
        live.managed_portfolios().mark([opening.model_copy(update={"value": 110 + 10 * month})])
        live.close_books(failed=False, mortgages=[])
        live_values.append(live.holding_value(HOUSEHOLD, live.mark_month))
    assert (stopped.rollout_id, live.rollout_id) == (1, 0)
    assert stopped_values == [100, 100]
    assert live_values == [100, 110, 120]
    assert (stopped.mark_month, live.mark_month) == (0, 2)
    stopped_marks = stopped.book().tlh_portfolios
    assert stopped_marks is not None
    [mark] = stopped_marks
    assert (mark.value, mark.reported_tax_basis, mark.portfolio_id) == (100, 80, "managed")
    assert stopped.managed_portfolios().marks == {"managed": opening}
    assert stopped.book().failed
    assert not live.book().failed


@pytest.mark.parametrize("case", ["duplicate", "mismatch"])
def test_opening_a_component_requires_one_matching_observation_per_declared_portfolio(
    spec: PreparedTlhPortfolio, opening: TlhPortfolioObservation, case: str
) -> None:
    books = accounting()
    managed = ManagedPortfolios(INCOME_SOURCES, ())
    if case == "duplicate":
        managed.open(books, spec, opening)
        with pytest.raises(ValueError, match="already open"):
            managed.open(books, spec, opening)
    else:
        with pytest.raises(ValueError, match="unknown ownership"):
            managed.open(books, spec, opening.model_copy(update={"account_id": "test_elsewhere"}))
        assert not managed.specs
    assert books.ledger.trial_balance() == 0


def test_world_rejects_an_unselected_rollout(spec: PreparedTlhPortfolio) -> None:
    with pytest.raises(ValueError, match="rollout selection"):
        composed(spec, 2)


if __name__ == "__main__":
    pytest_bazel.main()
