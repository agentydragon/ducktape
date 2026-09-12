"""Independent sale/tax controls driven by ordinary Python monthly actions.

The fixed sale dates belong to these experiments, not the execution input. Tax
assessment, exact lot accounting and payment still use the common action session.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.actions import Action, DecisionActions, LotSale, PayClaim, Sell, Transfer
from finance.augur.sim.books import AccountRef, TaxAccrual
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.observations import Decision
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedSeries,
    PreparedTransfer,
)
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction, Rollout
from finance.augur.sim.scenario import ORDINARY_INCOME, TaxProfile
from finance.augur.sim.session import ActionSession
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

# Federal single-filer schedule in sim/data/jurisdictions/federal_us.yaml.
# Expectations are independent amounts, not another invocation of tax arithmetic.
STANDARD_DEDUCTION = 1_460_000
CAPITAL_LOSS_OFFSET_CAP = 300_000
GAIN = 5_000_000
WAGES = Decimal(30_000)
QUIET, TAXED = 0, 1

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
QUANTUM = Decimal("0.01")
FEDERAL = "federal_us"


@dataclass(frozen=True)
class Situation:
    """The compiled paths, the one VTI lot Alice opens holding, and the month-zero wages she is paid."""

    series: tuple[PreparedSeries, ...]
    rollout_count: int
    horizon_months: int
    lot: PreparedLot
    wages: Decimal


def _situation(prices: np.ndarray, *, quantity: float, cost_basis: Decimal, wages: Decimal) -> Situation:
    """One stipulated `(rollout, month)` price block; the horizon is the snapshots it carries."""
    rollout_count, snapshots = prices.shape
    horizon = snapshots - 1
    paths = ExternalSeriesContext.from_level_blocks(
        [(VTI, prices)], rollout_count=rollout_count, horizon_months=horizon
    )
    scale = quantity_scale_for_asset(VTI)
    return Situation(
        series=compile_series(paths, rollout_count=rollout_count, horizon_months=horizon, currency_quantum=QUANTUM),
        rollout_count=rollout_count,
        horizon_months=horizon,
        lot=PreparedLot(
            lot_id="alice-vti",
            agent_id="alice",
            account_id="checking",
            asset_id=str(VTI.symbol),
            purchase_month=-24,
            quantity_scale=scale,
            units=int(quantity_to_quanta(quantity, scale=scale)),
            basis=int(currency_amount_to_quanta(cost_basis, quantum=QUANTUM)),
        ),
        wages=wages,
    )


def _compose(case: Situation, rollout_id: int) -> World:
    """Alice files federally from a cashless `checking` account; `employer` opens holding exactly the wages it pays."""
    federal = load_jurisdiction(FEDERAL)
    world = World(
        MarketPath(case.series, rollout_id, rollout_count=case.rollout_count),
        horizon_months=case.horizon_months,
        income_sources=(ORDINARY_INCOME,),
        jurisdictions=(PreparedJurisdiction(jurisdiction_id=FEDERAL, level=federal.level),),
    )
    openings = [("alice", Decimal(0)), ("irs", Decimal(0))]
    if case.wages:
        openings.append(("employer", case.wages))
    for agent_id, opening in openings:
        world.declare_account(
            PreparedAccount(
                account=AccountRef(agent_id=agent_id, account_id="checking"),
                opening_balance=int(currency_amount_to_quanta(opening, quantum=QUANTUM)),
            )
        )
    profile = TaxProfile(
        agent_id="alice", jurisdiction_ids=[FEDERAL], tax_authority_agent_id="irs", prior_year_tax=Decimal(0)
    )
    world.track(TaxAuthority(compile_profile(profile, {FEDERAL: federal}, quantum=QUANTUM)))
    world.declare_pool(
        PreparedHoldingPool(
            agent_id="alice", account_id="checking", asset_id=str(VTI.symbol), quantity_scale=case.lot.quantity_scale
        )
    )
    world.hold(case.lot)
    if case.wages:
        # Wages are the one cashflow an action cannot express: a bare actor transfer may not
        # declare tax character, so the payroll run is the scheduled table the world carries.
        world.scheduled_transfers = (
            PreparedTransfer(
                month=0,
                cause_id="wages",
                from_account=AccountRef(agent_id="employer", account_id="checking"),
                to_account=AccountRef(agent_id="alice", account_id="checking"),
                amount=int(currency_amount_to_quanta(case.wages, quantum=QUANTUM)),
                income_category=ORDINARY_INCOME,
                deduction_category=None,
            ),
        )
    return world


def _sell_and_pay(decisions: list[Decision], sale_month: int) -> list[DecisionActions]:
    responses = []
    for decision in decisions:
        observation = decision.observation
        actions: list[Action] = []
        if observation.month == sale_month:
            actions.append(
                Sell(
                    cause_id="sell-vti",
                    agent_id="alice",
                    proceeds_account_id="checking",
                    asset_id="vti",
                    lots=tuple(
                        LotSale(account_id=lot.account_id, lot_id=lot.lot_id, units=lot.units)
                        for lot in observation.public_positions
                        if lot.asset_id == "vti" and lot.account_id == "checking"
                    ),
                )
            )
        # Author order is sale, then this month's due payments. No engine allocator.
        actions.extend(
            PayClaim(
                request_id=index,
                cause_id=claim.cause_id,
                claim=claim,
                from_account=AccountRef(agent_id="alice", account_id="checking"),
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
        )
        responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
    return responses


def _run(case: Situation, *, sale_month: int, rollout_ids: list[int]) -> Finished:
    session = ActionSession({id_: _compose(case, id_) for id_ in rollout_ids}, "alice")
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(_sell_and_pay(batch, sale_month))
        return batch
    finally:
        session.close()


def test_sale_receipt_cannot_be_rewritten_through_policy_memory() -> None:
    case = _gain_situation(wages=Decimal(0))
    session = ActionSession({0: _compose(case, 0)}, "alice")
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        lots = [
            LotSale(account_id=lot.account_id, lot_id=lot.lot_id, units=lot.units)
            for lot in batch[0].observation.public_positions
        ]
        request = Sell(
            cause_id="sell-once", agent_id="alice", proceeds_account_id="checking", asset_id="vti", lots=tuple(lots)
        )
        batch = session.advance([DecisionActions(0, 0, [request])])
        assert not isinstance(batch, Finished)
        [receipt] = batch[0].observation.previous_receipts
        assert isinstance(receipt.action, Sell)
        expected = tuple(lots)
        lots.clear()
        recorded_lots: Any = receipt.action.lots
        with pytest.raises(TypeError, match="does not support item assignment"):
            recorded_lots[0] = LotSale(account_id="checking", lot_id="invented", units=1)
        assert receipt.action.lots == expected
        while not isinstance(batch, Finished):
            batch = session.advance(_sell_and_pay(batch, sale_month=-1))
        [rollout] = batch.rollouts
        assert rollout.trace is not None
        assert rollout.trace.receipts[0].action == request
        assert len(rollout.trace.events.lot_dispositions) == 1
        assert all(lot.units_remaining == 0 for lot in rollout.summary.ending_book.lots)
    finally:
        session.close()


def _gain_situation(*, wages: Decimal) -> Situation:
    return _situation(np.full((1, 13), 60_000.0), quantity=1.0, cost_basis=Decimal(10_000), wages=wages)


@pytest.fixture
def bare_gain() -> TaxAccrual:
    [rollout] = _run(_gain_situation(wages=Decimal(0)), sale_month=0, rollout_ids=[0]).rollouts
    assert rollout.stop is None
    [assessment] = rollout.summary.tax_accruals
    return assessment


@pytest.fixture
def wages_and_gain() -> TaxAccrual:
    [rollout] = _run(_gain_situation(wages=WAGES), sale_month=0, rollout_ids=[0]).rollouts
    assert rollout.stop is None
    [assessment] = rollout.summary.tax_accruals
    return assessment


def test_bare_gain_has_the_full_deduction_and_no_ordinary_income(bare_gain: TaxAccrual) -> None:
    assert bare_gain.standard_deduction == STANDARD_DEDUCTION
    assert bare_gain.long_term_gain == GAIN
    assert bare_gain.ordinary_income == 0
    assert bare_gain.month == 11


def test_unused_standard_deduction_shelters_long_term_gain(bare_gain: TaxAccrual) -> None:
    # §63 deducts $14,600 from $50,000; §1(h) rates the remaining $35,400 at 0%.
    assert bare_gain.total_tax == 0


@pytest.mark.parametrize(
    ("loss", "offset"),
    [(Decimal(2_000), 200_000), (Decimal(30_000), CAPITAL_LOSS_OFFSET_CAP)],
    ids=["under-the-cap", "over-the-cap"],
)
def test_capital_loss_offsets_ordinary_income_only_up_to_1211_cap(loss: Decimal, offset: int) -> None:
    case = _situation(np.full((1, 13), 1_000.0), quantity=1.0, cost_basis=Decimal(1_000) + loss, wages=Decimal(0))
    [rollout] = _run(case, sale_month=0, rollout_ids=[0]).rollouts
    assert rollout.stop is None
    [assessment] = rollout.summary.tax_accruals
    assert assessment.ordinary_income == -offset
    # §1212 carries the remainder, including zero when the whole loss fits the cap.
    assert assessment.capital_loss_carryforward == int(loss * 100) - offset


def test_stacking_case_is_wages_beside_gain(wages_and_gain: TaxAccrual) -> None:
    assert wages_and_gain.standard_deduction == STANDARD_DEDUCTION
    assert wages_and_gain.ordinary_income == 3_000_000
    assert wages_and_gain.long_term_gain == GAIN


def test_gain_is_rated_from_where_ordinary_income_leaves_off(wages_and_gain: TaxAccrual) -> None:
    # Ordinary: ($30,000 - $14,600) -> $1,616. Gain: $31,625 at 0%,
    # remaining $18,375 at 15% -> $2,756.25. Total is $4,372.25.
    assert wages_and_gain.total_tax == 437_225


@pytest.fixture
def independent_paths() -> Situation:
    prices = np.full((2, 26), 100.0)
    prices[TAXED, :] = 20_000.0
    return _situation(prices, quantity=10.0, cost_basis=Decimal(1_000), wages=Decimal(0))


@pytest.fixture
def population(independent_paths: Situation) -> dict[int, Rollout]:
    result = _run(independent_paths, sale_month=15, rollout_ids=[QUIET, TAXED])
    assert all(rollout.stop is None for rollout in result.rollouts)
    return {rollout.rollout_id: rollout for rollout in result.rollouts}


def test_only_spiked_path_owes_tax(population: dict[int, Rollout]) -> None:
    assert [tax.total_tax for tax in population[QUIET].summary.tax_accruals] == [0, 0]
    first_year, second_year = population[TAXED].summary.tax_accruals
    assert first_year.total_tax == 0
    assert second_year.total_tax > 0
    for rollout_id, proceeds in [(QUIET, 100_000), (TAXED, 20_000_000)]:
        trace = population[rollout_id].trace
        assert trace is not None
        [sale] = trace.events.lot_dispositions.to_dicts()
        assert sale["month_index"] == 15
        assert sale["proceeds_quanta"] == proceeds
        assert sale["cost_basis_consumed_quanta"] == 100_000
        assert trace.books[15].lots[0].units_remaining == 10 * trace.books[15].lots[0].quantity_scale
        assert trace.books[16].lots[0].units_remaining == 0


def test_liability_changes_and_settlement_belong_to_their_path(population: dict[int, Rollout]) -> None:
    # Opening/closing books use snapshot months: year-end events 11/23 appear
    # at 12/24, and the taxed path's event24 settlement appears at snapshot25.
    for rollout in population.values():
        trace = rollout.trace
        assert trace is not None
        assert [tax.month for tax in rollout.summary.tax_accruals] == [11, 23]
        for snapshot, year_end in [(12, 11), (24, 23)]:
            assert any(liability.tax_year_end_month == year_end for liability in trace.books[snapshot].tax_liabilities)
    quiet = population[QUIET]
    taxed_path = population[TAXED]
    assert quiet.summary.tax_settlements == []
    [settlement] = taxed_path.summary.tax_settlements
    assert settlement.month == 24
    assert settlement.tax_year_end_month == 23
    assert settlement.amount == taxed_path.summary.tax_accruals[1].total_tax
    assert quiet.trace is not None
    assert taxed_path.trace is not None
    # A different path's payment cannot manufacture a change in the quiet books.
    assert quiet.trace.books[24].tax_liabilities == quiet.trace.books[25].tax_liabilities
    assert any(liability.amount_owed > 0 for liability in taxed_path.trace.books[24].tax_liabilities)
    assert all(liability.amount_owed == 0 for liability in taxed_path.trace.books[25].tax_liabilities)


def test_selected_reordered_replay_matches_original_paths(
    independent_paths: Situation, population: dict[int, Rollout]
) -> None:
    reordered = _run(independent_paths, sale_month=15, rollout_ids=[TAXED, QUIET])
    assert [rollout.rollout_id for rollout in reordered.rollouts] == [TAXED, QUIET]
    assert reordered.rollouts == [population[TAXED], population[QUIET]]
    assert _run(independent_paths, sale_month=15, rollout_ids=[TAXED]).rollouts == [population[TAXED]]


def test_rejected_sale_preserves_successful_prefix_and_stops_only_its_path(independent_paths: Situation) -> None:
    session = ActionSession({id_: _compose(independent_paths, id_) for id_ in (TAXED, QUIET)}, "alice")
    observed: dict[int, list[int]] = {TAXED: [], QUIET: []}
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            for decision in batch:
                observed[decision.rollout_id].append(decision.observation.month)
            responses = _sell_and_pay(batch, sale_month=15)
            for index, response in enumerate(responses):
                if response.month == 15 and response.rollout_id == TAXED:
                    responses[index] = DecisionActions(
                        response.rollout_id,
                        response.month,
                        [
                            *response.actions,
                            Sell(
                                cause_id="sell-exhausted-lot",
                                agent_id="alice",
                                proceeds_account_id="checking",
                                asset_id="vti",
                                lots=(LotSale(account_id="checking", lot_id="alice-vti", units=1),),
                            ),
                            Transfer(
                                cause_id="unattempted",
                                from_account=AccountRef(agent_id="alice", account_id="checking"),
                                to_account=AccountRef(agent_id="irs", account_id="checking"),
                                amount=1,
                            ),
                        ],
                    )
            batch = session.advance(responses)
        stopped, completed = batch.rollouts
    finally:
        session.close()
    assert stopped.rollout_id == TAXED
    assert completed.rollout_id == QUIET
    assert isinstance(stopped.stop, RejectedAction)
    assert (stopped.stop.month, stopped.stop.action_index) == (15, 1)
    assert completed.stop is None
    assert observed[TAXED] == list(range(16))
    assert observed[QUIET] == list(range(25))
    sale, rejection = stopped.summary.last_receipts
    assert isinstance(sale.outcome, Executed)
    assert isinstance(rejection.outcome, Rejected)
    assert stopped.summary.cash[0].values[-1] == 20_000_000
    assert stopped.summary.ending_book.lots[0].units_remaining == 0
    assert stopped.trace is not None
    assert stopped.trace.events.transfers.is_empty()
    assert len(stopped.trace.books) == 17


if __name__ == "__main__":
    pytest_bazel.main()
