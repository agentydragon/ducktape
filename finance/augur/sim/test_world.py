"""Actor-scoped financial execution, ordered request prefixes and canonical capture."""

from copy import deepcopy
from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.sim.actions import Action, Buy, ClaimId, Consume, DecisionActions, LotSale, PayClaim, Sell, Transfer
from finance.augur.sim.capture import WorldResult
from finance.augur.sim.compiler.tax import PreparedTaxBracket
from finance.augur.sim.configured import execute
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedHoldingPool,
    PreparedLot,
    PreparedObligation,
    PreparedSeries,
    PreparedTransfer,
    _ScheduledSale,
)
from finance.augur.sim.results import ConsumptionTarget, Executed, Rejected
from finance.augur.sim.session import ActionSession, Capture, _Session
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, RESERVE, WORLD, prepared_scenario
from finance.augur.sim.world import World


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


def world_for(run: CompiledRun, mode: Capture = "forensic", rollout: int = 0) -> World:
    return World(run, rollout, [], capture_mode=mode, actor=HOUSEHOLD, product_actor=None)


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
    world.prepare_month(0, {}, {})
    world.assemble_claims([])
    observation = world.observe(HOUSEHOLD)
    assert len(observation.holding_pools) == 1
    assert observation.holding_pools[0].account_id == "empty-brokerage"
    assert observation.holding_pools[0].price == 1000
    assert not observation.public_positions
    action = buy("first-purchase", "checking", 2_000_000).model_copy(
        update={"holding_account_id": "empty-brokerage", "lot_id": "new-position"}
    )
    assert isinstance(world.apply(HOUSEHOLD, action, 0), Executed)
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    world.prepare_month(1, {}, {})
    world.assemble_claims([])
    observation = world.observe(HOUSEHOLD)
    assert observation.holding_pools[0].price == 2000
    assert (observation.public_holdings, observation.cash) == (4000, 500)
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    financial = world.finish([]).financial
    assert financial is not None
    [lot] = financial.months[-1].lots
    assert (lot.units_remaining, lot.basis_remaining, lot.account_id) == (2_000_000, 2000, "empty-brokerage")
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in financial.journal)


def test_declaring_an_empty_pool_does_not_invest_cash_without_an_action(cash_only: CompiledRun) -> None:
    world = world_for(cash_only)
    for month in range(2):
        world.prepare_month(month, {}, {})
        world.assemble_claims([])
        world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    summary = world.finish([]).summary
    assert summary is not None
    assert not summary.ending_book.lots
    assert summary.cash[0].values == [2500] * 3


@pytest.mark.parametrize("wrong_scale", [False, True])
def test_an_empty_pool_purchase_rejects_wrong_account_or_scale_without_mutation(
    cash_only: CompiledRun, wrong_scale: bool
) -> None:
    world = world_for(cash_only)
    world.prepare_month(0, {}, {})
    world.assemble_claims([])
    action = buy("invalid-purchase", "checking", 1).model_copy(
        update={
            "holding_account_id": "empty-brokerage" if wrong_scale else "other-brokerage",
            "quantity_scale": 10 if wrong_scale else 1_000_000,
        }
    )
    before = deepcopy(
        (world.book([]), world.accounting.journal, world.holdings.dispositions, world.accounting.journal_entry_count)
    )
    assert isinstance(world.apply(HOUSEHOLD, action, 0), Rejected)
    assert (
        world.book([]),
        world.accounting.journal,
        world.holdings.dispositions,
        world.accounting.journal_entry_count,
    ) == before
    assert world.account_balance(HOUSEHOLD, "checking") == 2500
    world.close_month(failed=True, shortfall=0, mortgages=[], snapshots=[])
    financial = world.finish([]).financial
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
        ActionSession(run, HOUSEHOLD, [0])


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
    for month in range(13):
        world.prepare_month(month, {}, {})
        world.assemble_claims([])
        observation = world.observe(HOUSEHOLD)
        assert observation.cash == (20_000 if month == 0 else 0)
        assert len(observation.claims) == (1 if month in (0, 12) else 0)
        for claim in observation.claims:
            assert claim.month == month
            assert claim.amount_due == (50_000 if month == 0 else 1500)
        units = 30_000_000 if month == 0 else 1_500_000 if month == 12 else 0
        if units:
            assert isinstance(world.apply(HOUSEHOLD, sell(units), 0), Executed)
        for index, claim in enumerate(observation.claims):
            action = PayClaim(
                request_id=7,
                cause_id=f"pay-{claim.cause_id}",
                claim=ClaimId(month=claim.month, index=claim.index),
                from_account=claim.from_account,
                amount=claim.amount_due,
            )
            assert isinstance(world.apply(HOUSEHOLD, action, index + 1), Executed)
        assert not world.unpaid_claims(HOUSEHOLD)
        world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    financial = world.finish([]).financial
    assert financial is not None
    assert len(financial.months) == 14
    sale = financial.dispositions[0]
    assert (sale.proceeds, sale.basis, sale.realized_gain) == (30_000, 15_000, 15_000)
    assert financial.tax_payments[0].amount_paid == 1500
    assert [row.amount_paid for row in financial.obligations] == [50_000, 1500]
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in financial.journal)


def test_ordered_actions_can_buy_before_transferring_and_buy_again() -> None:
    world = world_for(actor_run())
    world.prepare_month(0, {}, {})
    world.assemble_claims([])
    actions: list[Action] = [
        sell(20_000_000),
        buy("first-buy", "checking", 10_000_000),
        transfer(15_000),
        buy("second-buy", "savings", 15_000_000),
        consume(5000),
    ]
    for index, action in enumerate(actions):
        assert isinstance(world.apply(HOUSEHOLD, action, index), Executed)
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    world.prepare_month(1, {}, {})
    world.assemble_claims([])
    observation = world.observe(HOUSEHOLD)
    assert (observation.cash, observation.public_holdings) == (0, 105_000)
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    financial = world.finish([]).financial
    assert financial is not None
    chosen = [action.cause_id for action in actions]
    assert [entry.cause_id for entry in financial.journal if entry.cause_id in chosen] == chosen


def test_rejected_financial_request_preserves_prior_sale_and_independent_world() -> None:
    run = actor_run(3, 2)
    failed, live = world_for(run), world_for(run, rollout=1)
    failed.prepare_month(0, {}, {})
    failed.assemble_claims([])
    assert isinstance(failed.apply(HOUSEHOLD, sell(10_000_000), 0), Executed)
    assert isinstance(failed.apply(HOUSEHOLD, buy("impossible", "checking", 1_000_000_000), 1), Rejected)
    failed.close_month(failed=True, shortfall=0, mortgages=[], snapshots=[])
    for month in range(3):
        live.prepare_month(month, {}, {})
        live.assemble_claims([])
        assert isinstance(live.apply(HOUSEHOLD, consume(1000), 0), Executed)
        live.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    failed_output, live_output = failed.finish([]).financial, live.finish([]).financial
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
    world.prepare_month(0, {}, {})
    world.assemble_claims([])
    [claim] = world.observe(HOUSEHOLD).claims
    assert isinstance(world.apply(HOUSEHOLD, transfer(5000), 0), Executed)
    action = PayClaim(
        request_id=11,
        cause_id="pay-from-reserve",
        claim=ClaimId(month=claim.month, index=claim.index),
        from_account=RESERVE,
        amount=5000,
    )
    assert isinstance(world.apply(HOUSEHOLD, action, 1), Executed)
    world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
    output = world.finish([])
    assert output.financial is not None
    assert output.summary is not None
    [payment] = output.financial.obligations
    assert (payment.from_account, payment.amount_paid) == (RESERVE, 5000)
    assert output.summary.payments[0].from_account == RESERVE


def test_compact_capture_replays_observed_prefixes_and_canonical_payment_identity() -> None:
    run = actor_run(3)
    run = replace(run, series=(replace(run.series[0], values=(1000, 2000, 9000, 10_000)),))
    outputs = []
    modes: tuple[Capture, ...] = ("summary", "dense", "forensic")
    for mode in modes:
        world = world_for(run, mode)
        for month in range(2):
            world.prepare_month(month, {}, {})
            world.assemble_claims([])
            assert isinstance(world.apply(HOUSEHOLD, sell(1_000_000), 0), Executed)
            outcome = world.apply(HOUSEHOLD, consume(1_000_000 if month == 1 else 1000), 1)
            assert isinstance(outcome, Rejected) == (month == 1)
            world.close_month(failed=month == 1, shortfall=0, mortgages=[], snapshots=[])
        outputs.append(world.finish([]))
    compact = outputs[0].summary
    assert compact is not None
    assert outputs[0].financial is None
    for output in outputs[1:]:
        assert compact == output.summary
        assert output.financial is not None
        assert compact.ending_book == output.financial.months[-1]
        for payment in compact.payments:
            assert (payment.action_index, payment.receipt.request_id, payment.from_account) == (1, 3, CASH)
            assert payment.target is not None
            assert payment.target.to_account == EXOGENOUS
            assert payment.receipt.target == ConsumptionTarget(component_id="flex-budget")
            assert payment.receipt.amount_paid == sum(
                row.amount_paid for row in output.financial.obligations if row.month == payment.month
            )
    assert (compact.ending_book.month, compact.ending_mark_month) == (2, 1)
    assert compact.public_holdings[0].values == [100_000, 198_000, 196_000]
    assert all(len(cash.values) == 3 for cash in compact.cash)
    assert compact.ending_book.lots[0].units_remaining == 98_000_000
    assert compact.payments[1].receipt.amount_paid == 0
    assert compact.cash[0].values[-1] == 12_000


@pytest.mark.parametrize("mode", ["summary", "forensic"])
def test_unpaid_claims_keep_occurrence_and_source_without_hidden_sales(mode: Capture) -> None:
    run = actor_run(3)
    world = world_for(replace(run, scenario=replace(run.scenario, obligations=(bill(5000), bill(7000)))), mode)
    world.prepare_month(0, {}, {})
    world.assemble_claims([])
    unpaid = world.unpaid_claims(HOUSEHOLD)
    assert len(unpaid) == 2
    assert unpaid[0].id != unpaid[1].id
    world.close_month(failed=True, shortfall=0, mortgages=[], snapshots=[])
    output = world.finish([])
    summary = output.summary
    assert summary is not None
    first, second = summary.unpaid_claims
    assert first.id != second.id
    assert first.cause_id == second.cause_id
    assert (first.from_account, first.to_account, first.amount_due, second.amount_due) == (CASH, EXOGENOUS, 5000, 7000)
    assert not summary.payments
    assert summary.cash[0].values == [10_000, 10_000]
    if output.financial is not None:
        assert not output.financial.dispositions
        assert not output.financial.transfers
        assert all(row.amount_paid == 0 for row in output.financial.obligations)


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
    untouched = deepcopy(first.book([]))
    for world, units, tax_paid, basis in ((second, 10_000_000, 3000, 40_000), (first, 20_000_000, 2000, 30_000)):
        for month in range(13):
            world.prepare_month(month, {}, {})
            world.assemble_claims([])
            if month == 0:
                for index, lot in enumerate(run.scenario.initial_lots):
                    action = Sell(
                        cause_id=f"sell-{lot.asset_id}",
                        agent_id=HOUSEHOLD,
                        proceeds_account_id="checking",
                        asset_id=lot.asset_id,
                        lots=(LotSale(account_id=lot.account_id, lot_id=lot.lot_id, units=units),),
                    )
                    assert isinstance(world.apply(HOUSEHOLD, action, index), Executed)
            settlement = world.settle_claims()
            assert not settlement.failed
            world.close_month(failed=False, shortfall=0, mortgages=[], snapshots=[])
        output = world.finish([]).financial
        assert output is not None
        assert output.failed_month is None
        assert [(p.month, p.amount_paid) for p in output.tax_payments] == [(12, tax_paid)]
        assert sum(e.cause_id == f"opening:{HOUSEHOLD}:checking" for e in output.journal) == 1
        assert world.account_balance(HOUSEHOLD, "checking") == 5000 - tax_paid
        assert sum(lot.basis_remaining for lot in output.months[-1].lots) == basis
        assert output.months[-1].month == 13
        if world is second:
            assert first.book([]) == untouched
            assert not first.accounting.tax_liabilities
    assert run == before


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("mode", ["forensic", "dense", "summary"])
def test_month_stepping_preserves_tax_year_and_stopped_books_in_every_capture_mode(
    year_run: CompiledRun, stopped: bool, mode: Capture
) -> None:
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
    run = replace(run, scenario=replace(run.scenario, _scheduled_sales=sales))
    [baseline] = execute(run, mode, HOUSEHOLD)
    session = _Session(run, HOUSEHOLD, [0], capture=mode, configured=True, product_actor=HOUSEHOLD)
    try:
        session.start()
        path = session.paths[0]
        while not session.is_finished():
            if session.month == 12:
                assert path.world.accounting.tax_liabilities[0].amount_owed == (50 if stopped else 2000)
            session.begin_actions([DecisionActions(0, session.month, [])])
            for sale in sales:
                if sale.month == session.month:
                    path.world.holdings.scheduled_sale(path.world.accounting, path.world.market, sale)
            settlement = path.world.settle_claims()
            path.failed, path.shortfall = settlement.failed, settlement.product_shortfall
            session.close_month()
        assert path.result is not None
        assert_same_result(path.result, baseline)
        terminal = deepcopy(replace(path.result, events=None))
        if path.result.events is not None:
            terminal = replace(
                terminal,
                events=EventLog.from_frames(
                    {spec.name: path.result.events.frame(spec).clone() for spec in EVENT_FRAME_SPECS},
                    rollout_ids=path.result.events.rollout_ids,
                ),
            )
        stopped_book = deepcopy(path.world.book([]))
        session.close_month()
        assert_same_result(path.result, terminal)
        assert path.world.book([]) == stopped_book
        with pytest.raises(ValueError, match="finished"):
            session.begin_actions([DecisionActions(0, session.month, [])])
        assert_same_result(path.result, terminal)
        assert path.world.book([]) == stopped_book
        if mode == "summary":
            assert baseline.configured_summary is not None
            assert baseline.configured_summary.failed_month == (12 if stopped else None)
        else:
            assert baseline.financial is not None
            assert baseline.financial.failed_month == (12 if stopped else None)
        if baseline.financial is not None:
            assert len(baseline.financial.months) == 14
            assert [(p.month, p.amount_paid) for p in baseline.financial.tax_payments] == (
                [(12, 0)] if stopped else [(12, 2000)]
            )
            if mode == "dense":
                assert not baseline.financial.journal
    finally:
        session.close()


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
            _scheduled_sales=(
                _ScheduledSale(
                    month=1,
                    cause_id="sell-stock",
                    agent_id=HOUSEHOLD,
                    account_id="checking",
                    asset_id="test_stock",
                    units=1_000_000,
                    proceeds_account_id="checking",
                ),
            ),
        ),
        series=(replace(run.series[0], values=(10_000, 15_000, 15_000, 10_000, 20_000, 20_000)),),
    )
    baseline = execute(run, "forensic", HOUSEHOLD)
    outputs = execute(run, mode, HOUSEHOLD)
    assert len(baseline) == len(outputs) == 2
    for index, output in enumerate(outputs):
        expected = baseline[index]
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


if __name__ == "__main__":
    pytest_bazel.main()
