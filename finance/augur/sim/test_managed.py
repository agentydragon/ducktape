"""Component financial facts settle atomically without recreating private holdings."""

from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Withdraw
from finance.augur.sim.books import AccountRef
from finance.augur.sim.holdings import gain_account
from finance.augur.sim.managed import ComponentEffects, InterestCredit, ManagedPortfolios, basis_account
from finance.augur.sim.money import MIN_COUNT
from finance.augur.sim.observations import TlhPortfolioObservation
from finance.augur.sim.prepared import CompiledRun, PreparedLot, PreparedSeries, PreparedTlhPortfolio
from finance.augur.sim.testing.accounting import CASH, HOUSEHOLD, prepared_books, prepared_scenario
from finance.augur.sim.tlh import TlhAssumptions
from finance.augur.sim.world import World


@pytest.fixture
def run() -> CompiledRun:
    scenario = replace(
        prepared_scenario(),
        horizon_months=2,
        tlh_portfolios=(
            PreparedTlhPortfolio(
                portfolio_id="managed",
                owner_agent_id=HOUSEHOLD,
                account_id="custody",
                asset_id="test_fund",
                quantity_scale=1,
                # One unit bought at 80 and priced at 100 opens the component at value 100, basis 80.
                initial_cohorts=(
                    PreparedLot(
                        lot_id="managed-opening",
                        agent_id=HOUSEHOLD,
                        account_id="custody",
                        asset_id="test_fund",
                        purchase_month=-1,
                        quantity_scale=1,
                        units=1,
                        basis=80,
                    ),
                ),
                assumptions=TlhAssumptions(
                    peak_annual_yield=0,
                    floor_annual_yield=0,
                    maturity_decay_exponent=1,
                    drawdown_sensitivity=0,
                    short_term_fraction=1,
                ),
            ),
        ),
    )
    return CompiledRun(
        currency_code="USD",
        currency_quantum="0.01",
        rollout_count=2,
        scenario=scenario,
        series=(PreparedSeries(series_id="security:test_fund", snapshots=3, values=(100, 110, 120) * 2),),
    )


@pytest.fixture
def opening() -> TlhPortfolioObservation:
    return TlhPortfolioObservation(
        portfolio_id="managed",
        owner_agent_id=HOUSEHOLD,
        account_id="custody",
        asset_id="test_fund",
        value=100,
        reported_tax_basis=80,
    )


@pytest.fixture
def world(run: CompiledRun) -> World:
    return World.from_run(run, 0)


def books(run: CompiledRun) -> Accounting:
    return prepared_books(run.scenario)


def fingerprint(world: World) -> tuple[object, ...]:
    return deepcopy(
        (
            dict(world.accounting.ledger.balances),
            world.accounting.tax.years,
            world.accounting.tax.income.by_source,
            world.accounting.journal,
            world.accounting.transfers,
            world.managed.marks,
            world.managed.effects,
            world.managed.distributions,
            world.holdings.lots,
        )
    )


def harvest(opening: TlhPortfolioObservation) -> ComponentEffects:
    return ComponentEffects(opening.model_copy(update={"reported_tax_basis": 70}), None, 0, -10, 0)


def test_basis_statement_cash_and_tax_reconcile_without_ordinary_lots(
    world: World, opening: TlhPortfolioObservation
) -> None:
    world.managed.settle(world.accounting, 0, HOUSEHOLD, "manager", harvest(opening), operation="modeled_realization")
    contribution = ComponentEffects(
        opening.model_copy(update={"value": 120, "reported_tax_basis": 90}), "checking", -20, 0, 0
    )
    world.managed.settle(world.accounting, 0, HOUSEHOLD, "contribution", contribution, operation="contribution")
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
        effects = ComponentEffects(opening.model_copy(update={"reported_tax_basis": 181}), "checking", -101, 0, 0)
    else:
        amount = 10 if case == 6 else -10
        effects = ComponentEffects(
            opening, "checking", amount, 0, 0, (InterestCredit("undeclared" if case == 6 else None, amount),)
        )
    before = fingerprint(world)
    with pytest.raises(
        (ValueError, OverflowError), match=r"reconcile|observation|overflow|available cash|declared income"
    ):
        world.managed.settle(world.accounting, 0, HOUSEHOLD, "manager", effects, operation="modeled_realization")
    assert fingerprint(world) == before


def test_distribution_cash_uses_interest_source_not_capital_gain_journal_account(
    world: World, opening: TlhPortfolioObservation
) -> None:
    effects = ComponentEffects(opening, "checking", 5, 0, 0, (InterestCredit(None, 5),))
    world.managed.settle(world.accounting, 0, HOUSEHOLD, "distribution", effects, operation="distribution")
    assert world.accounting.ledger.balance(CASH) == 105
    assert world.accounting.ledger.balance(gain_account(HOUSEHOLD)) == 0
    assert world.accounting.ledger.balance(AccountRef(agent_id="__external__", account_id="boundary")) == -5
    assert (
        world.accounting.tax.years[HOUSEHOLD].short_term_gain,
        world.accounting.tax.years[HOUSEHOLD].long_term_gain,
    ) == (0, 0)
    assert world.managed.marks["managed"].reported_tax_basis == 80


def test_withdrawal_receipt_does_not_recalculate_component_rounded_value(
    run: CompiledRun, opening: TlhPortfolioObservation
) -> None:
    accounting = books(run)
    managed = ManagedPortfolios(run.scenario.income_sources, run.scenario.jurisdictions)
    [spec] = run.scenario.tlh_portfolios
    managed.open(accounting, spec, opening.model_copy(update={"value": 2, "reported_tax_basis": 2}))
    effects = ComponentEffects(opening.model_copy(update={"value": 0, "reported_tax_basis": 0}), "checking", 1, 0, -1)
    action = Withdraw(
        cause_id="redemption", agent_id=HOUSEHOLD, portfolio_id="managed", cash_account_id="checking", amount=1
    )
    managed.settle(accounting, 0, HOUSEHOLD, "redemption", effects, operation="redemption", action=action)
    assert managed.marks["managed"].value == 0
    assert accounting.ledger.balance(CASH) == 101
    assert accounting.tax.years[HOUSEHOLD].long_term_gain == -1
    assert accounting.ledger.trial_balance() == 0


def test_component_marks_keep_explicit_stop_marks_and_independent_books(
    run: CompiledRun, opening: TlhPortfolioObservation
) -> None:
    stopped, live = World.from_run(run, 1), World.from_run(run, 0)
    stopped.prepare_month(0, {}, {})
    stopped.assemble_claims([])
    stopped.managed.mark([opening])
    stopped_values = [stopped.holding_value(HOUSEHOLD, stopped.mark_month)]
    stopped.close_books(failed=True, mortgages=[])
    stopped_values.append(stopped.holding_value(HOUSEHOLD, stopped.mark_month))
    live_values = [live.holding_value(HOUSEHOLD, live.mark_month)]
    for month in range(2):
        live.prepare_month(month, {}, {})
        live.assemble_claims([])
        live.managed.mark([opening.model_copy(update={"value": 110 + 10 * month})])
        live.close_books(failed=False, mortgages=[])
        live_values.append(live.holding_value(HOUSEHOLD, live.mark_month))
    assert (stopped.rollout_id, live.rollout_id) == (1, 0)
    assert stopped_values == [100, 100]
    assert live_values == [100, 110, 120]
    assert (stopped.mark_month, live.mark_month) == (0, 2)
    [mark] = stopped.book().tlh_portfolios
    assert (mark.value, mark.reported_tax_basis, mark.portfolio_id) == (100, 80, "managed")
    assert stopped.managed.marks == {"managed": opening}
    assert stopped.book().failed
    assert not live.book().failed


@pytest.mark.parametrize("case", ["duplicate", "mismatch"])
def test_opening_a_component_requires_one_matching_observation_per_declared_portfolio(
    run: CompiledRun, opening: TlhPortfolioObservation, case: str
) -> None:
    accounting = books(run)
    managed = ManagedPortfolios(run.scenario.income_sources, run.scenario.jurisdictions)
    [spec] = run.scenario.tlh_portfolios
    if case == "duplicate":
        managed.open(accounting, spec, opening)
        with pytest.raises(ValueError, match="already open"):
            managed.open(accounting, spec, opening)
    else:
        with pytest.raises(ValueError, match="unknown ownership"):
            managed.open(accounting, spec, opening.model_copy(update={"account_id": "test_elsewhere"}))
        assert not managed.specs
    assert accounting.ledger.trial_balance() == 0


def test_world_rejects_an_unselected_rollout(run: CompiledRun) -> None:
    with pytest.raises(ValueError, match="rollout selection"):
        World.from_run(run, 2)


if __name__ == "__main__":
    pytest_bazel.main()
