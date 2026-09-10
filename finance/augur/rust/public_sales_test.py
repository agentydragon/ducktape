"""Independent sale/tax controls driven by ordinary Python monthly actions.

The fixed sale dates belong to these experiments, not the execution input. Tax
assessment, exact lot accounting and payment still use the common action session.
"""

import json
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.rust.simulator import Action, ActionSession, Decision, DecisionActions
from finance.augur.sim.books import TaxAccrual
from finance.augur.sim.results import Executed, Finished, Rejected, RejectedAction, Rollout
from finance.augur.sim.scenario import InitialLot, OrdinaryIncome, ScheduledTransfer
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import VTI, checking, taxed

# Federal single-filer schedule in sim/data/jurisdictions/federal_us.yaml.
# Expectations are independent amounts, not another invocation of tax arithmetic.
STANDARD_DEDUCTION = 1_460_000
CAPITAL_LOSS_OFFSET_CAP = 300_000
GAIN = 5_000_000
WAGES = Decimal(30_000)
QUIET, TAXED = 0, 1


def _sell_and_pay(decisions: list[Decision], sale_month: int) -> list[DecisionActions]:
    responses = []
    for decision in decisions:
        observation = decision.observation
        actions = []
        if observation.month == sale_month:
            actions.append(
                Action.sell(
                    cause_id="sell-vti",
                    agent_id="alice",
                    proceeds_account_id="checking",
                    asset_id="vti",
                    lots=[
                        (lot.account_id, lot.lot_id, lot.units)
                        for lot in observation.public_positions
                        if lot.asset_id == "vti" and lot.account_id == "checking"
                    ],
                )
            )
        # Author order is sale, then this month's due payments. No engine allocator.
        actions.extend(
            Action.pay_claim(
                request_id=index,
                cause_id=claim.cause_id,
                claim=claim,
                from_account=("alice", "checking"),
                amount=claim.amount_due,
            )
            for index, claim in enumerate(observation.claims)
        )
        responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
    return responses


def _run(case: Case, *, sale_month: int, rollout_ids: list[int]) -> Finished:
    session = ActionSession(json.dumps(case.compiled_run.execution_input), "alice", rollout_ids)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(_sell_and_pay(batch, sale_month))
        return batch
    finally:
        session.close()


def _gain_case(*, wages: Decimal) -> Case:
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0)), ("employer", wages)),
            scheduled_transfers=[
                ScheduledTransfer(
                    month=0,
                    cause_id="wages",
                    from_agent_id="employer",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=wages,
                    income_category=OrdinaryIncome(),
                )
            ]
            if wages
            else [],
            horizon_months=12,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=1.0,
                    cost_basis_per_unit=Decimal(10_000),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        series={VTI: levels([[Decimal(60_000)] * 13])},
    )


@pytest.fixture
def bare_gain() -> TaxAccrual:
    [rollout] = _run(_gain_case(wages=Decimal(0)), sale_month=0, rollout_ids=[0]).rollouts
    assert rollout.stop is None
    [assessment] = rollout.summary.tax_accruals
    return assessment


@pytest.fixture
def wages_and_gain() -> TaxAccrual:
    [rollout] = _run(_gain_case(wages=WAGES), sale_month=0, rollout_ids=[0]).rollouts
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
    case = Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=12,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=1.0,
                    cost_basis_per_unit=Decimal(1_000) + loss,
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        series={VTI: levels([[Decimal(1_000)] * 13])},
    )
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
def independent_paths() -> Case:
    prices = np.full((2, 26), 100.0)
    prices[TAXED, :] = 20_000.0
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=25,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,
                    quantity=10.0,
                    cost_basis_per_unit=Decimal(100),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=2,
        series={VTI: prices},
    )


@pytest.fixture
def population(independent_paths: Case) -> dict[int, Rollout]:
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
    independent_paths: Case, population: dict[int, Rollout]
) -> None:
    reordered = _run(independent_paths, sale_month=15, rollout_ids=[TAXED, QUIET])
    assert [rollout.rollout_id for rollout in reordered.rollouts] == [TAXED, QUIET]
    assert reordered.rollouts == [population[TAXED], population[QUIET]]
    assert _run(independent_paths, sale_month=15, rollout_ids=[TAXED]).rollouts == [population[TAXED]]


def test_rejected_sale_preserves_successful_prefix_and_stops_only_its_path(independent_paths: Case) -> None:
    session = ActionSession(json.dumps(independent_paths.compiled_run.execution_input), "alice", [TAXED, QUIET])
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
                            Action.sell(
                                cause_id="sell-exhausted-lot",
                                agent_id="alice",
                                proceeds_account_id="checking",
                                asset_id="vti",
                                lots=[("checking", "alice-vti", 1)],
                            ),
                            Action.transfer("unattempted", ("alice", "checking"), ("irs", "checking"), 1),
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
