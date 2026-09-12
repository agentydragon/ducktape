"""Actor-scoped financial execution, ordered request prefixes and canonical capture."""

from copy import deepcopy
from dataclasses import replace
from itertools import pairwise

import pytest
import pytest_bazel

from finance.augur.product.household import ConfiguredHousehold
from finance.augur.sim.actions import Action, Buy, ClaimId, Consume, DecisionActions, LotSale, PayClaim, Sell, Transfer
from finance.augur.sim.actor import MonthOpened
from finance.augur.sim.agent import EconomicAgent, assemble
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.capture import FinancialCapture, WorldResult, event_log
from finance.augur.sim.compiler.tax import PreparedTaxBracket
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.ids import AgentId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.mortgage import Mortgage, MortgageTerms
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedRecurringObligation,
    PreparedSeries,
    PreparedTransfer,
    _ScheduledSale,
)
from finance.augur.sim.product_metrics import product_row
from finance.augur.sim.results import ConsumptionTarget, Executed, Finished, Rejected, RejectedAction, UnpaidClaims
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, RESERVE, WORLD, prepared_scenario
from finance.augur.sim.world import Capture, World


def actor_run(horizon: int = 2, paths: int = 1) -> CompiledRun:
    base = prepared_scenario()
    scenario = replace(
        base,
        horizon_months=horizon,
        tax_profiles=(),
        accounts=tuple(
            replace(account, opening_balance=10_000 if account.account == CASH else 0) for account in base.accounts
        ),
        holding_pools=(
            PreparedHoldingPool(
                agent_id=HOUSEHOLD, account_id="checking", asset_id="test_stock", quantity_scale=1_000_000
            ),
        ),
        initial_lots=(
            PreparedLot(
                lot_id="timing-stock",
                agent_id=HOUSEHOLD,
                account_id="checking",
                asset_id="test_stock",
                purchase_month=-24,
                quantity_scale=1_000_000,
                units=100_000_000,
                basis=50_000,
            ),
        ),
    )
    return CompiledRun(
        currency_code="USD",
        currency_quantum="0.01",
        rollout_count=paths,
        scenario=scenario,
        series=(
            PreparedSeries(
                series_id="security:test_stock", snapshots=horizon + 1, values=(1000,) * ((horizon + 1) * paths)
            ),
        ),
    )


@pytest.fixture
def cash_only() -> CompiledRun:
    run = actor_run()
    return replace(
        run,
        scenario=replace(
            run.scenario,
            initial_lots=(),
            accounts=tuple(
                replace(account, opening_balance=2500 if account.account == CASH else 0)
                for account in run.scenario.accounts
            ),
            holding_pools=(
                replace(run.scenario.holding_pools[0], account_id="empty-brokerage"),
                PreparedHoldingPool(
                    agent_id=WORLD, account_id="other-brokerage", asset_id="test_stock", quantity_scale=1_000_000
                ),
            ),
        ),
        series=(replace(run.series[0], values=(1000, 2000, 3000)),),
    )


def world_for(run: CompiledRun, rollout: int = 0) -> World:
    world = World.from_run(run, rollout)
    world.start()
    return world


def checking(world: World) -> int | None:
    return world.account_balance(HOUSEHOLD, "checking")


def view(world: World) -> Observation:
    """The household's assembled view of the open month, as a tracked agent would see it."""
    return assemble(HOUSEHOLD, world.month, world.open_mail(HOUSEHOLD))


def next_month(world: World, capture: FinancialCapture, *, failed: bool = False) -> None:
    """Close the open month the way `step` does, record it, and open the next one unless finished."""
    world.failed = world.failed or failed
    world.close_month()
    capture.record()
    if not world.finished:
        world.open_month()


def sell(units: int) -> Sell:
    return Sell(
        cause_id="chosen-sale",
        agent_id=HOUSEHOLD,
        proceeds_account_id="checking",
        asset_id="test_stock",
        lots=(LotSale(account_id="checking", lot_id="timing-stock", units=units),),
    )


def buy(id_: str, cash: str, units: int) -> Buy:
    return Buy(
        cause_id=id_,
        agent_id=HOUSEHOLD,
        cash_account_id=cash,
        holding_account_id="checking",
        asset_id="test_stock",
        lot_id=id_,
        quantity_scale=1_000_000,
        units=units,
    )


def transfer(amount: int) -> Transfer:
    return Transfer(cause_id="move-cash", from_account=CASH, to_account=RESERVE, amount=amount)


def consume(amount: int) -> Consume:
    return Consume(
        request_id=3,
        cause_id="chosen-consumption",
        component_id="flex-budget",
        from_account=CASH,
        to_account=EXOGENOUS,
        amount=amount,
    )


def bill(amount: int) -> PreparedObligation:
    return PreparedObligation(
        month=0,
        obligation_id="bill",
        obligation_type="rent",
        from_account=CASH,
        to_account=EXOGENOUS,
        amount_due=amount,
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def test_cash_only_actor_observes_and_purchases_an_unheld_declared_asset(cash_only: CompiledRun) -> None:
    world = world_for(cash_only)
    capture = FinancialCapture(world, capture="forensic")
    observation = view(world)
    assert len(observation.holding_pools) == 1
    assert observation.holding_pools[0].account_id == "empty-brokerage"
    assert observation.holding_pools[0].price == 1000
    assert not observation.public_positions
    action = buy("first-purchase", "checking", 2_000_000).model_copy(
        update={"holding_account_id": "empty-brokerage", "lot_id": "new-position"}
    )
    assert isinstance(world.execute(HOUSEHOLD, action).outcome, Executed)
    next_month(world, capture)
    observation = view(world)
    assert observation.holding_pools[0].price == 2000
    assert (observation.public_holdings, observation.cash) == (4000, 500)
    next_month(world, capture)
    financial = capture.financial()
    assert financial is not None
    [lot] = financial.months[-1].lots
    assert (lot.units_remaining, lot.basis_remaining, lot.account_id) == (2_000_000, 2000, "empty-brokerage")
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in financial.journal)


def test_declaring_an_empty_pool_does_not_invest_cash_without_an_action(cash_only: CompiledRun) -> None:
    world = world_for(cash_only)
    capture = FinancialCapture(world, capture="summary")
    balances = [checking(world)]
    for _ in range(2):
        next_month(world, capture)
        balances.append(checking(world))
    assert world.finished
    assert not world.book().lots
    assert balances == [2500] * 3


@pytest.mark.parametrize("wrong_scale", [False, True])
def test_an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation(
    cash_only: CompiledRun, wrong_scale: bool
) -> None:
    world = world_for(cash_only)
    capture = FinancialCapture(world, capture="forensic")
    action = buy("invalid-purchase", "checking", 1).model_copy(
        update={
            "holding_account_id": "empty-brokerage" if wrong_scale else "other-brokerage",
            "quantity_scale": 10 if wrong_scale else 1_000_000,
        }
    )
    before = deepcopy((world.book(), world.accounting.journal, world.holdings.dispositions))
    assert isinstance(world.execute(HOUSEHOLD, action).outcome, Rejected)
    assert (world.book(), world.accounting.journal, world.holdings.dispositions) == before
    assert checking(world) == 2500
    next_month(world, capture)
    assert world.finished
    financial = capture.financial()
    assert financial is not None
    assert not financial.months[-1].lots
    assert all(entry.cause_id != "invalid-purchase" for entry in financial.journal)


@pytest.mark.parametrize("case", ["no_pool", "unpriced", "duplicate"])
def test_declarations_reject_missing_prices_and_do_not_fall_back_to_initial_lots(case: str) -> None:
    run = actor_run(1)
    pool = run.scenario.holding_pools[0]
    if case == "no_pool":
        run = replace(run, scenario=replace(run.scenario, holding_pools=()))
    elif case == "unpriced":
        run = replace(
            run, scenario=replace(run.scenario, initial_lots=(), holding_pools=(replace(pool, asset_id="unpriced"),))
        )
    else:
        run = replace(run, scenario=replace(run.scenario, initial_lots=(), holding_pools=(pool, pool)))
    with pytest.raises(ValueError, match=r"holding pool|missing series"):
        ActionSession.from_run(run, HOUSEHOLD, [0])


def test_cashflows_claims_sales_and_cross_year_tax_share_financial_books() -> None:
    run = actor_run(13)
    profile = prepared_scenario().tax_profiles[0]
    rules = replace(
        profile.jurisdictions[0],
        ordinary_brackets=(PreparedTaxBracket(None, 200_000_000),),
        long_term_capital_gain_brackets=(PreparedTaxBracket(None, 100_000_000),),
        max_capital_loss_ordinary_offset=0,
    )
    run = replace(
        run,
        scenario=replace(
            run.scenario,
            tax_profiles=(replace(profile, jurisdictions=(rules,)),),
            obligations=(bill(50_000),),
            accounts=tuple(replace(account, opening_balance=0) for account in run.scenario.accounts),
            scheduled_transfers=(
                PreparedTransfer(
                    month=0,
                    cause_id="current-contribution",
                    from_account=EXOGENOUS,
                    to_account=CASH,
                    amount=20_000,
                    income_category=None,
                    deduction_category=None,
                ),
            ),
        ),
    )
    world = world_for(run)
    capture = FinancialCapture(world, capture="forensic")
    for month in range(13):
        observation = view(world)
        assert observation.cash == (20_000 if month == 0 else 0)
        assert len(observation.claims) == (1 if month in (0, 12) else 0)
        for claim in observation.claims:
            assert claim.month == month
            assert claim.amount_due == (50_000 if month == 0 else 1500)
        units = 30_000_000 if month == 0 else 1_500_000 if month == 12 else 0
        if units:
            assert isinstance(world.execute(HOUSEHOLD, sell(units)).outcome, Executed)
        for claim in observation.claims:
            action = PayClaim(
                request_id=7,
                cause_id=f"pay-{claim.cause_id}",
                claim=ClaimId(month=claim.month, index=claim.index),
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            assert isinstance(world.execute(HOUSEHOLD, action).outcome, Executed)
        assert not world.unpaid_claims(HOUSEHOLD)
        next_month(world, capture)
    financial = capture.financial()
    assert financial is not None
    assert len(financial.months) == 14
    sale = financial.dispositions[0]
    assert (sale.proceeds, sale.basis, sale.realized_gain) == (30_000, 15_000, 15_000)
    assert financial.tax_payments[0].amount_paid == 1500
    assert [row.amount_paid for row in financial.obligations] == [50_000, 1500]
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in financial.journal)


def test_ordered_actions_can_buy_before_transferring_and_buy_again() -> None:
    world = world_for(actor_run())
    capture = FinancialCapture(world, capture="forensic")
    actions: list[Action] = [
        sell(20_000_000),
        buy("first-buy", "checking", 10_000_000),
        transfer(15_000),
        buy("second-buy", "savings", 15_000_000),
        consume(5000),
    ]
    for action in actions:
        assert isinstance(world.execute(HOUSEHOLD, action).outcome, Executed)
    next_month(world, capture)
    observation = view(world)
    assert (observation.cash, observation.public_holdings) == (0, 105_000)
    next_month(world, capture)
    financial = capture.financial()
    assert financial is not None
    chosen = [action.cause_id for action in actions]
    assert [entry.cause_id for entry in financial.journal if entry.cause_id in chosen] == chosen


def test_rejected_financial_request_preserves_prior_sale_and_independent_world() -> None:
    run = actor_run(3, 2)
    failed, live = world_for(run), world_for(run, rollout=1)
    failed_capture, live_capture = FinancialCapture(failed, capture="dense"), FinancialCapture(live, capture="dense")
    assert isinstance(failed.execute(HOUSEHOLD, sell(10_000_000)).outcome, Executed)
    assert isinstance(failed.execute(HOUSEHOLD, buy("impossible", "checking", 1_000_000_000)).outcome, Rejected)
    next_month(failed, failed_capture)
    assert failed.finished
    for _ in range(3):
        assert isinstance(live.execute(HOUSEHOLD, consume(1000)).outcome, Executed)
        next_month(live, live_capture)
    failed_output, live_output = failed_capture.financial(), live_capture.financial()
    assert failed_output is not None
    assert live_output is not None
    assert len(failed_output.dispositions) == 1
    assert not failed_output.transfers
    assert len(failed_output.months) == 2
    assert (failed_output.months[1].lots[0].units_remaining, failed_output.months[1].lots[0].basis_remaining) == (
        90_000_000,
        45_000,
    )
    assert len(live_output.months) == 4
    assert len(live_output.obligations) == 3
    assert live_output.months[-1].lots[0].units_remaining == 100_000_000


def test_payment_capture_names_the_actual_selected_source() -> None:
    run = actor_run(1)
    world = world_for(replace(run, scenario=replace(run.scenario, obligations=(bill(5000),))))
    [claim] = view(world).claims
    assert isinstance(world.execute(HOUSEHOLD, transfer(5000)).outcome, Executed)
    action = PayClaim(
        request_id=11,
        cause_id="pay-from-reserve",
        claim=ClaimId(month=claim.month, index=claim.index),
        from_account=RESERVE,
        amount=5000,
    )
    assert isinstance(world.execute(HOUSEHOLD, action).outcome, Executed)
    world.close_month()
    [obligation] = world.obligations
    assert (obligation.from_account, obligation.amount_paid) == (RESERVE, 5000)
    assert world.payments[0].from_account == RESERVE


def test_compact_capture_replays_observed_prefixes_and_canonical_payment_identity() -> None:
    run = actor_run(3)
    run = replace(run, series=(replace(run.series[0], values=(1000, 2000, 9000, 10_000)),))
    outputs = []
    modes: tuple[Capture, ...] = ("summary", "dense", "forensic")
    for mode in modes:
        session = ActionSession.from_run(run, HOUSEHOLD, [0], capture=mode)
        batch = session.start()
        for month in range(2):
            assert not isinstance(batch, Finished)
            actions: list[Action] = [sell(1_000_000), consume(1_000_000 if month == 1 else 1000)]
            batch = session.advance([DecisionActions(0, month, actions)])
        assert isinstance(batch, Finished)
        outputs.append(batch.rollouts[0])
        session.close()
    compact = outputs[0].summary
    assert outputs[0].trace is None
    for output in outputs[1:]:
        assert compact == output.summary
        assert output.trace is not None
        assert compact.ending_book == output.trace.books[-1]
        for payment in compact.payments:
            assert (payment.action_index, payment.receipt.request_id, payment.from_account) == (1, 3, CASH)
            assert payment.target is not None
            assert payment.target.to_account == EXOGENOUS
            assert payment.receipt.target == ConsumptionTarget(component_id="flex-budget")
    assert outputs[1].stop == outputs[2].stop == RejectedAction(month=1, action_index=1)
    assert (compact.ending_book.month, compact.ending_mark_month) == (2, 1)
    assert compact.public_holdings[0].values == [100_000, 198_000, 196_000]
    assert all(len(cash.values) == 3 for cash in compact.cash)
    assert compact.ending_book.lots[0].units_remaining == 98_000_000
    assert compact.payments[1].receipt.amount_paid == 0
    assert compact.cash[0].values[-1] == 12_000
    assert outputs[2].trace is not None
    assert outputs[1].trace is not None
    assert outputs[2].trace.journal
    assert not outputs[1].trace.journal


@pytest.mark.parametrize("mode", ["summary", "forensic"])
def test_unpaid_claims_keep_occurrence_and_source_without_hidden_sales(mode: Capture) -> None:
    run = actor_run(3)
    world = world_for(replace(run, scenario=replace(run.scenario, obligations=(bill(5000), bill(7000)))))
    capture = FinancialCapture(world, capture=mode)
    unpaid = world.unpaid_claims(HOUSEHOLD)
    assert len(unpaid) == 2
    assert unpaid[0].id != unpaid[1].id
    next_month(world, capture, failed=True)
    assert world.finished
    first, second = world.unpaid_claims(HOUSEHOLD)
    assert first.id != second.id
    assert first.cause_id == second.cause_id
    assert (first.from_account, first.to_account, first.amount_due, second.amount_due) == (CASH, EXOGENOUS, 5000, 7000)
    assert not world.payments
    assert checking(world) == 10_000
    financial = capture.financial()
    assert (financial is None) == (mode == "summary")
    if financial is not None:
        assert not financial.dispositions
        assert not financial.transfers
        assert all(row.amount_paid == 0 for row in financial.obligations)


def assert_same_result(actual: WorldResult, expected: WorldResult) -> None:
    # Compare every result field; Polars frames need explicit value equality.
    assert replace(actual, events=None) == replace(expected, events=None)
    if expected.events is None:
        assert actual.events is None
    else:
        assert actual.events is not None
        assert actual.events.rollout_ids == expected.events.rollout_ids
        for spec in EVENT_FRAME_SPECS:
            assert actual.events.frame(spec).equals(expected.events.frame(spec))


@pytest.fixture
def year_run() -> CompiledRun:
    run = actor_run(13)
    lot = replace(run.scenario.initial_lots[0], units=50_000_000, basis=25_000)
    profile = prepared_scenario().tax_profiles[0]
    rules = replace(
        profile.jurisdictions[0],
        ordinary_brackets=(PreparedTaxBracket(None, 200_000_000),),
        long_term_capital_gain_brackets=(PreparedTaxBracket(None, 100_000_000),),
        max_capital_loss_ordinary_offset=0,
    )
    return replace(
        run,
        scenario=replace(
            run.scenario,
            initial_lots=(lot, replace(lot, lot_id="second-lot", asset_id="second")),
            holding_pools=(*run.scenario.holding_pools, replace(run.scenario.holding_pools[0], asset_id="second")),
            tax_profiles=(replace(profile, jurisdictions=(rules,)),),
            obligations=(bill(50_000), replace(bill(5000), month=12)),
            scheduled_transfers=(
                PreparedTransfer(
                    month=12,
                    cause_id="test-contribution",
                    from_account=EXOGENOUS,
                    to_account=CASH,
                    amount=10_000,
                    income_category=None,
                    deduction_category=None,
                ),
            ),
        ),
        series=(*run.series, replace(run.series[0], series_id="security:second")),
    )


def test_retained_rollouts_keep_opening_books_lots_and_tax_state_independent(year_run: CompiledRun) -> None:
    run = replace(
        year_run,
        rollout_count=2,
        series=tuple(replace(s, values=(*s.values, *(v * 2 for v in s.values))) for s in year_run.series),
    )
    before = deepcopy(run)
    first, second = world_for(run, rollout=0), world_for(run, rollout=1)
    untouched = deepcopy(first.book())
    for world, units, tax_paid, basis in ((second, 10_000_000, 3000, 40_000), (first, 20_000_000, 2000, 30_000)):
        capture = FinancialCapture(world, capture="forensic")
        for month in range(13):
            if month == 0:
                for lot in run.scenario.initial_lots:
                    action = Sell(
                        cause_id=f"sell-{lot.asset_id}",
                        agent_id=HOUSEHOLD,
                        proceeds_account_id="checking",
                        asset_id=lot.asset_id,
                        lots=(LotSale(account_id=lot.account_id, lot_id=lot.lot_id, units=units),),
                    )
                    assert isinstance(world.execute(HOUSEHOLD, action).outcome, Executed)
            settlement = world.settle_claims()
            assert not settlement.failed
            next_month(world, capture)
        output = capture.financial()
        assert output is not None
        assert output.failed_month is None
        assert [(p.month, p.amount_paid) for p in output.tax_payments] == [(12, tax_paid)]
        assert sum(e.cause_id == f"opening:{HOUSEHOLD}:checking" for e in output.journal) == 1
        assert checking(world) == 5000 - tax_paid
        assert sum(lot.basis_remaining for lot in output.months[-1].lots) == basis
        assert output.months[-1].month == 13
        if world is second:
            assert first.book() == untouched
            assert not first.accounting.tax_liabilities
    assert run == before


def composed(run: CompiledRun, rollout: int = 0) -> World:
    """The prepared run's facts declared one at a time, as an experiment would write them."""
    scenario = run.scenario
    world = World(
        MarketPath.from_run(run, rollout),
        horizon_months=scenario.horizon_months,
        income_sources=scenario.income_sources,
        jurisdictions=scenario.jurisdictions,
    )
    for account in scenario.accounts:
        world.declare_account(account)
    for profile in scenario.tax_profiles:
        world.track(TaxAuthority(profile))
    for pool in scenario.holding_pools:
        world.declare_pool(pool)
    for lot in scenario.initial_lots:
        world.hold(lot)
    world.scheduled_transfers = scenario.scheduled_transfers
    for obligation in scenario.obligations:
        world.track(Biller(obligation))
    return world


def recorded(world: World, mode: Capture) -> tuple[FinancialCapture, list[tuple[int, int, int, int, int, int, int]]]:
    return FinancialCapture(world, capture=mode), [product_row(world, HOUSEHOLD)]


def result_of(
    world: World, capture: FinancialCapture, mode: Capture, rows: list[tuple[int, int, int, int, int, int, int]]
) -> WorldResult:
    financial = capture.financial()
    return WorldResult(
        world.rollout_id,
        financial,
        event_log(financial) if financial is not None else None,
        capture.configured_summary() if mode == "summary" else None,
        rows,
    )


def stepped(
    run: CompiledRun, mode: Capture, *, rollout: int = 0, sales: tuple[_ScheduledSale, ...] = ()
) -> WorldResult:
    """One composed rollout to its horizon under a household that makes the scheduled sales."""
    world = composed(run, rollout)
    world.track(ConfiguredHousehold(HOUSEHOLD, (), scheduled_sales=sales))
    capture, rows = recorded(world, mode)
    world.start()
    while not world.finished:
        world.step()
        capture.record()
        rows.append(product_row(world, HOUSEHOLD))
    return result_of(world, capture, mode, rows)


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("mode", ["forensic", "dense", "summary"])
def test_step_is_the_explicit_phases_and_keeps_the_tax_year_and_stopped_books(
    year_run: CompiledRun, stopped: bool, mode: Capture
) -> None:
    """`step` is open, decide, execute in order, close — and nothing else the phases do not do."""
    run = year_run
    sales = tuple(
        _ScheduledSale(
            month=0,
            cause_id=f"sale-{lot.asset_id}",
            agent_id=HOUSEHOLD,
            account_id=lot.account_id,
            asset_id=lot.asset_id,
            units=20_000_000,
            proceeds_account_id="checking",
        )
        for lot in run.scenario.initial_lots
    )
    if stopped:
        run = replace(
            run,
            scenario=replace(
                run.scenario, horizon_months=15, obligations=(bill(1000), replace(bill(999_999), month=12))
            ),
            series=tuple(replace(s, snapshots=16, values=(*s.values, 9000, 10_000)) for s in run.series),
        )
        sales = (replace(sales[0], units=1_000_000),)

    def household() -> ConfiguredHousehold:
        return ConfiguredHousehold(HOUSEHOLD, (), scheduled_sales=sales)

    phased = composed(run)
    actor = household()
    phased.track(actor)
    phased_capture, phased_rows = recorded(phased, mode)
    phased.start()
    while not phased.finished:
        actions = actor.handle(MonthOpened(month=phased.month))
        phased.begin_actions(actions)
        for action in actions:
            if isinstance(phased.execute(HOUSEHOLD, action).outcome, Rejected):
                break
        phased.close_month()
        phased_capture.record()
        phased_rows.append(product_row(phased, HOUSEHOLD))
        if not phased.finished:
            phased.open_month()

    path = composed(run)
    path.track(household())
    capture, rows = recorded(path, mode)
    path.start()
    while not path.finished:
        if path.month == 12:
            assert path.accounting.tax_liabilities[0].amount_owed == (50 if stopped else 2000)
        path.step()
        capture.record()
        rows.append(product_row(path, HOUSEHOLD))
    result = result_of(path, capture, mode, rows)
    assert_same_result(result, result_of(phased, phased_capture, mode, phased_rows))

    stopped_book = deepcopy(path.book())
    with pytest.raises(ValueError, match="finished"):
        path.open_month()
    assert path.book() == stopped_book
    if mode == "summary":
        assert result.configured_summary is not None
        assert result.configured_summary.failed_month == (12 if stopped else None)
    else:
        assert result.financial is not None
        assert result.financial.failed_month == (12 if stopped else None)
    if result.financial is not None:
        assert len(result.financial.months) == 14
        assert [(p.month, p.amount_paid) for p in result.financial.tax_payments] == (
            [(12, 0)] if stopped else [(12, 2000)]
        )
        if mode == "dense":
            assert not result.financial.journal


@pytest.mark.parametrize("mode", ["forensic", "dense", "summary"])
def test_transfer_and_fifo_sale_remain_balanced(mode: Capture) -> None:
    run = actor_run(2, 2)
    run = replace(
        run,
        scenario=replace(
            run.scenario,
            accounts=tuple(
                replace(a, opening_balance=1000 if a.account == CASH else 2000 if a.account == EXOGENOUS else 0)
                for a in run.scenario.accounts
            ),
            initial_lots=(replace(run.scenario.initial_lots[0], units=2_000_000, basis=20_000),),
            scheduled_transfers=(
                PreparedTransfer(
                    month=0,
                    cause_id="gift",
                    from_account=EXOGENOUS,
                    to_account=CASH,
                    amount=500,
                    income_category=None,
                    deduction_category=None,
                ),
            ),
        ),
        series=(replace(run.series[0], values=(10_000, 15_000, 15_000, 10_000, 20_000, 20_000)),),
    )
    sales = (
        _ScheduledSale(
            month=1,
            cause_id="sell-stock",
            agent_id=HOUSEHOLD,
            account_id="checking",
            asset_id="test_stock",
            units=1_000_000,
            proceeds_account_id="checking",
        ),
    )
    for index in range(2):
        expected = stepped(run, "forensic", rollout=index, sales=sales)
        output = stepped(run, mode, rollout=index, sales=sales)
        assert output.product_metrics == expected.product_metrics
        financial = expected.financial
        assert financial is not None
        [sale] = financial.dispositions
        if mode == "summary":
            assert output.financial is None
            assert output.configured_summary is not None
            assert output.configured_summary.rollout_id == index
            assert output.configured_summary.ending_balances == financial.months[-1].balances
            assert output.configured_summary.failed_month is None
            assert output.configured_summary.disposition_count == len(financial.dispositions) == 1
            assert output.configured_summary.journal_entry_count == len(financial.journal)
        else:
            assert output.financial is not None
            assert output.financial.rollout_id == financial.rollout_id == index
            assert output.financial.months == financial.months
            assert output.financial.dispositions == financial.dispositions
            assert output.financial.journal == ([] if mode == "dense" else financial.journal)
        proceeds = (15_000, 20_000)[index]
        assert (sale.proceeds, sale.basis, sale.units, sale.realized_gain) == (
            proceeds,
            10_000,
            1_000_000,
            proceeds - 10_000,
        )
        assert next(b.balance for b in financial.months[-1].balances if b.account == CASH) == 1500 + proceeds
        assert all(sum(p.amount for p in entry.postings) == 0 for entry in financial.journal)
        if mode == "summary":
            assert output.financial is None
        elif mode == "dense":
            assert output.financial is not None
            assert output.financial == replace(financial, journal=[])
        else:
            assert output.financial == financial


class _Household(EconomicAgent):
    """Pays every due claim, then consumes a scripted amount in the months that have one."""

    def __init__(self, amounts: dict[int, int], agent_id: AgentId = HOUSEHOLD) -> None:
        super().__init__(agent_id)
        self.amounts = amounts
        self.months: list[int] = []
        self.dues: list[tuple[int, str, int]] = []

    def decide(self, observation: Observation) -> list[Action]:
        self.months.append(observation.month)
        self.dues.extend((observation.month, claim.obligation_type, claim.amount_due) for claim in observation.claims)
        actions: list[Action] = [
            PayClaim(
                request_id=index,
                cause_id="pay-bill",
                claim=claim,
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
        ]
        if observation.month in self.amounts:
            actions.append(consume(self.amounts[observation.month]))
        return actions


def test_tracked_agent_steps_agree_with_the_batch_session() -> None:
    run = actor_run(horizon=3, paths=2)
    run = replace(run, scenario=replace(run.scenario, obligations=(bill(1000),)))
    amounts = {0: 500, 2: 700}
    world = World.from_run(run, 1)
    agent = _Household(amounts)
    world.track(agent)
    world.start()
    balances = [checking(world)]
    while not world.finished:
        world.step()
        balances.append(checking(world))
    assert agent.months == [0, 1, 2]
    session = ActionSession.from_run(run, HOUSEHOLD, [1], capture="forensic")
    batch = session.start()
    while not isinstance(batch, Finished):
        [decision] = batch
        actions = _Household(amounts).decide(decision.observation)
        batch = session.advance([DecisionActions(1, decision.observation.month, actions)])
    [rollout] = batch.rollouts
    assert world.stop is rollout.stop is None
    assert world.book() == rollout.summary.ending_book
    assert world.previous_receipts == rollout.summary.last_receipts
    assert balances == rollout.summary.cash[0].values


def test_rejected_action_stops_the_path_before_later_decisions() -> None:
    world = World.from_run(actor_run(horizon=3), 0)
    agent = _Household({0: 100, 1: 10_000_000, 2: 100})
    world.track(agent)
    world.start()
    while not world.finished:
        world.step()
    assert agent.months == [0, 1]
    assert world.stop == RejectedAction(month=1, action_index=0)
    assert world.mark_month == 1
    with pytest.raises(ValueError, match="not running"):
        world.step()


def test_tracking_is_checked_before_the_world_starts() -> None:
    world = World.from_run(actor_run(), 0)
    with pytest.raises(ValueError, match="not running"):
        world.step()
    with pytest.raises(ValueError, match="unknown actor"):
        world.track(_Household({}, agent_id=AgentId("test-nobody")))
    world.track(_Household({}))
    with pytest.raises(ValueError, match="one decision-making agent"):
        world.track(_Household({}))
    world.start()
    with pytest.raises(ValueError, match="before starting"):
        world.track(_Household({}))
    with pytest.raises(ValueError, match="already started"):
        world.start()
    untracked = World.from_run(actor_run(), 0)
    untracked.start()
    with pytest.raises(ValueError, match="tracked agent"):
        untracked.step()


def loan(opening_principal: int | None = 6000) -> Mortgage:
    """One year at 12% on 6000: the level installment is 533."""
    return Mortgage(
        MortgageTerms(
            liability_id="test-loan",
            property_id="test-home",
            borrower=CASH,
            lender=EXOGENOUS,
            origination_month=0,
            origination_principal=6000,
            annual_interest_rate_ppb=120_000_000,
            term_months=12,
        ),
        opening_principal=opening_principal,
    )


def test_tracked_mortgage_is_serviced_from_the_ledger_through_payoff() -> None:
    world = World.from_run(actor_run(horizon=14), 0)
    household, mortgage = _Household({}), loan()
    world.track(household)
    world.track(mortgage)
    world.start()
    receivable = AccountRef(agent_id=WORLD, account_id="asset:mortgage-receivable:test-loan")
    assert (world.mortgage_principal("test-loan"), world.accounting.ledger.balance(receivable)) == (6000, 6000)
    principals, interest_ytd = [6000], []
    paid: list[tuple[int, int, int]] = []
    while not world.finished:
        world.step()
        assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in world.accounting.journal)
        paid.extend((row.month, row.interest, row.principal) for row in world.accounting.mortgage_payments)
        [state] = world.book().mortgages
        principals.append(state.principal)
        interest_ytd.append(state.interest_paid_ytd)
    assert world.stop is None
    assert world.accounting.ledger.trial_balance() == 0
    assert [due for due in household.dues if due[1] == "mortgage_payment"] == [
        (month, "mortgage_payment", interest + principal) for month, interest, principal in paid
    ]
    # Twelve level installments leave a rounding residual, paid by a short thirteenth.
    assert [month for month, _, _ in paid] == list(range(1, len(paid) + 1))
    assert paid[0] == (1, 60, 473)
    assert 0 < paid[-1][1] + paid[-1][2] < 533
    assert principals[:2] == [6000, 6000]
    assert [before - after for before, after in pairwise(principals[1 : len(paid) + 2])] == [
        principal for _, _, principal in paid
    ]
    assert principals[-1] == 0
    assert sum(principal for _, _, principal in paid) == 6000
    # Year-to-date interest accrues through November, resets at the December close and restarts.
    assert interest_ytd[10] == sum(interest for month, interest, _ in paid if month <= 10)
    assert interest_ytd[11] == 0
    assert interest_ytd[12] == paid[11][1]
    [state] = world.book().mortgages
    assert (state.active, world.accounting.ledger.balance(receivable)) == (False, 0)
    assert checking(world) == 10_000 - sum(interest + principal for _, interest, principal in paid)


def rent() -> Biller:
    """Rent of 500 due in months 1 and 2."""
    return Biller(
        PreparedRecurringObligation(
            obligation_id="test-rent",
            obligation_type="rent",
            from_account=CASH,
            to_account=EXOGENOUS,
            amount_due=500,
            property_id=None,
            deduction_category=None,
            deductible_fraction_ppb=1_000_000_000,
            start_month=1,
            end_month=2,
        )
    )


def test_a_tracked_bill_is_demanded_in_its_months_and_paid_by_the_household() -> None:
    world = World.from_run(actor_run(horizon=4), 0)
    household = _Household({})
    world.track(household)
    world.track(rent())
    world.start()
    while not world.finished:
        world.step()
    assert world.stop is None
    assert household.dues == [(1, "rent", 500), (2, "rent", 500)]
    assert checking(world) == 9000


def test_a_composed_world_has_only_the_domains_it_declares() -> None:
    run = actor_run(horizon=2)
    world = World(MarketPath.from_run(run, 0), horizon_months=2)
    for account in run.scenario.accounts:
        world.declare_account(account)
    for pool in run.scenario.holding_pools:
        world.declare_pool(pool)
    for lot in run.scenario.initial_lots:
        world.hold(lot)
    world.track(_Household({0: 500}))
    capture = FinancialCapture(world, capture="forensic")
    world.start()
    assert (world.bonds, world.properties, world.managed, world.private_equity, world.distributions) == (None,) * 5
    while not world.finished:
        world.step()
        capture.record()
    assert world.stop is None
    assert checking(world) == 9500
    book = world.book()
    assert (book.bonds, book.properties, book.mortgages, book.tlh_portfolios) == ([], [], [], [])
    financial = capture.financial()
    assert financial is not None
    assert not financial.bond_cashflows
    assert not financial.property_purchases
    assert not financial.tlh_financial_effects
    with pytest.raises(ValueError, match="no managed portfolio"):
        world.managed_portfolios()
    with pytest.raises(ValueError, match="before starting"):
        world.declare_pool(run.scenario.holding_pools[0])
    with pytest.raises(ValueError, match="missing public security series"):
        World(MarketPath.from_run(run, 0), horizon_months=2).declare_pool(
            replace(run.scenario.holding_pools[0], asset_id="test-unpriced")
        )


class _Deadbeat(EconomicAgent):
    def decide(self, observation: Observation) -> list[Action]:
        return []


def test_an_unpaid_installment_stops_the_path_and_leaves_the_contract_open() -> None:
    world = World.from_run(actor_run(horizon=3), 0)
    mortgage = loan()
    world.track(_Deadbeat(HOUSEHOLD))
    world.track(mortgage)
    world.start()
    world.step()
    assert world.stop is None
    world.step()
    assert world.finished
    assert world.stop == UnpaidClaims(month=1, claims=[ClaimId(month=1, index=0)])
    assert (mortgage.active, world.mortgage_principal("test-loan"), checking(world)) == (True, 6000, 10_000)


def test_tracked_bills_name_a_declared_payer_and_no_property() -> None:
    world = World.from_run(actor_run(), 0)
    with pytest.raises(ValueError, match="property"):
        world.track(Biller(replace(rent().spec, property_id="test-home")))
    with pytest.raises(ValueError, match="unknown actor"):
        world.track(
            Biller(replace(rent().spec, from_account=AccountRef(agent_id="test-nobody", account_id="checking")))
        )
    with pytest.raises(ValueError, match="not declared"):
        world.track(Biller(replace(rent().spec, from_account=AccountRef(agent_id=HOUSEHOLD, account_id="test-none"))))
    world.track(rent())
    world.start()
    with pytest.raises(ValueError, match="before starting"):
        world.track(rent())


def test_tracked_mortgages_open_the_ledger_once_before_the_world_starts() -> None:
    world = World.from_run(actor_run(), 0)
    with pytest.raises(ValueError, match="outstanding"):
        world.track(loan(None))
    with pytest.raises(ValueError, match="unknown actor"):
        world.track(
            Mortgage(replace(loan().terms, borrower=AccountRef(agent_id="test-nobody", account_id="checking")), 6000)
        )
    with pytest.raises(ValueError, match="not declared"):
        world.track(
            Mortgage(replace(loan().terms, borrower=AccountRef(agent_id=HOUSEHOLD, account_id="test-none")), 6000)
        )
    world.track(loan())
    with pytest.raises(ValueError, match="duplicate"):
        world.track(loan())
    world.start()
    with pytest.raises(ValueError, match="before starting"):
        world.track(Mortgage(replace(loan().terms, liability_id="test-second"), 6000))
    with pytest.raises(ValueError, match="servicing statement"):
        loan().handle(MonthOpened(month=0))


if __name__ == "__main__":
    pytest_bazel.main()
