"""Exercise real Python-owned batches, routing errors, receipt feedback and fatal stops."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel

from finance.augur.sim.actions import Action, ClaimId, Consume, DecisionActions, PayClaim, Transfer
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedObligation, PreparedTransfer
from finance.augur.sim.results import Executed, Finished, RejectedAction, Rollout, UnpaidClaims
from finance.augur.sim.scenario import ORDINARY_INCOME, ObligationType
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
ROLLOUT_COUNT = 2
ALICE = AccountRef(agent_id="alice", account_id="checking")
WORLD = AccountRef(agent_id="world", account_id="checking")


def quanta(amount: Decimal) -> int:
    return int(currency_amount_to_quanta(amount, quantum=QUANTUM))


def compose(rollout_id: int, *, obligation_id: str = "one-cent-bill") -> World:
    """Five cents against a two-cent opening contribution and a one-cent bill, both at month zero.

    Nominal only: no security, distribution or CPI series is supplied, so the market path
    carries nothing and the world moves cash and settles due claims alone.
    """
    world = World(
        MarketPath((), rollout_id, rollout_count=ROLLOUT_COUNT), horizon_months=4, income_sources=(ORDINARY_INCOME,)
    )
    for account, balance in ((ALICE, Decimal("0.05")), (WORLD, Decimal(0))):
        world.declare_account(PreparedAccount(account=account, opening_balance=quanta(balance)))
    world.scheduled_transfers = (
        PreparedTransfer(
            month=0,
            cause_id="opening-contribution",
            from_account=WORLD,
            to_account=ALICE,
            amount=quanta(Decimal("0.02")),
            income_category=None,
            deduction_category=None,
        ),
    )
    world.track(
        Biller(
            PreparedObligation(
                month=0,
                obligation_id=obligation_id,
                obligation_type=ObligationType.OUTSIDE_RENT,
                from_account=ALICE,
                to_account=WORLD,
                amount_due=quanta(Decimal("0.01")),
                property_id=None,
                deduction_category=None,
                deductible_fraction_ppb=1_000_000_000,
            )
        )
    )
    return world


def session(ids: Sequence[int], **parts: Any) -> ActionSession:
    return ActionSession({id_: compose(id_) for id_ in ids}, "alice", **parts)


def consume(amount: int, cause: str = "chosen-spend") -> Action:
    return Consume(
        request_id=0,
        cause_id=cause,
        component_id="budget",
        from_account=AccountRef(agent_id="alice", account_id="checking"),
        to_account=AccountRef(agent_id="world", account_id="checking"),
        amount=amount,
    )


def run(ids: list[int]) -> tuple[list[Rollout], dict[int, list[tuple[int, int]]]]:
    live = session(ids)
    observed: dict[int, list[tuple[int, int]]] = {id_: [] for id_ in ids}
    memory = dict.fromkeys(ids, 0)
    try:
        batch = live.start()
        while not isinstance(batch, Finished):
            responses = []
            for decision in reversed(batch):
                observation = decision.observation
                observed[decision.rollout_id].append((observation.month, observation.cash))
                receipts = observation.previous_receipts
                assert all(receipt.month == observation.month - 1 for receipt in receipts)
                assert all(isinstance(receipt.outcome, Executed) for receipt in receipts)
                memory[decision.rollout_id] += sum(isinstance(receipt.action, Consume) for receipt in receipts)
                assert memory[decision.rollout_id] == observation.month
                assert observation.accounts == (("checking", observation.cash),)
                assert observation.agent_id == "alice"
                assert observation.cpi is None  # This nominal-only experiment supplied no CPI model.
                assert not observation.public_positions
                actions: list[Action] = [
                    PayClaim(
                        request_id=i,
                        cause_id="pay-bill",
                        claim=claim,
                        from_account=claim.from_account,
                        amount=claim.amount_due,
                    )
                    for i, claim in enumerate(observation.claims)
                ]
                actions.append(consume(1))
                responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
            batch = live.advance(responses)
        return batch.rollouts, observed
    finally:
        live.close()


def test_current_facts_receipt_memory_and_original_replay() -> None:
    baseline, observed = run([0, 1])
    assert observed == {id_: [(0, 7), (1, 5), (2, 4), (3, 3)] for id_ in [0, 1]}
    for ids in [[1, 0], [1], [0], [0, 1]]:
        actual, replay_observed = run(ids)
        assert actual == [baseline[id_] for id_ in ids]
        assert replay_observed == {id_: observed[id_] for id_ in ids}
    assert [row.rollout_id for row in baseline] == [0, 1]
    assert all(row.stop is None for row in baseline)


def test_action_order_prefix_retention_and_independent_continuation() -> None:
    live = session([0, 1])
    batch = live.start()
    assert not isinstance(batch, Finished)
    responses = []
    for decision in batch:
        claim = decision.observation.claims[0]
        actions: list[Action] = [
            PayClaim(
                request_id=0, cause_id="pay", claim=claim, from_account=claim.from_account, amount=claim.amount_due
            )
        ]
        if decision.rollout_id == 0:
            actions += [
                Transfer(
                    cause_id="prefix",
                    from_account=AccountRef(agent_id="alice", account_id="checking"),
                    to_account=AccountRef(agent_id="world", account_id="checking"),
                    amount=1,
                ),
                consume(100),
                consume(1, "unattempted-suffix"),
            ]
        responses.append(DecisionActions(decision.rollout_id, 0, actions))
    batch = live.advance(responses)
    for month in range(1, 4):
        assert not isinstance(batch, Finished)
        assert [decision.rollout_id for decision in batch] == [1]
        assert batch[0].observation.month == month
        batch = live.advance([DecisionActions(1, month, [])])
    assert isinstance(batch, Finished)
    stopped, completed = batch.rollouts
    assert stopped.stop == RejectedAction(month=0, action_index=2)
    assert stopped.trace is not None
    assert [row.action.kind for row in stopped.trace.receipts] == ["PayClaim", "Transfer", "Consume"]
    closing = stopped.trace.books[-1]
    assert next(row.balance for row in closing.balances if row.account.agent_id == "alice") == 5
    assert completed.stop is None
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        live.advance([])


def test_unpaid_due_claim_is_not_an_implicit_payment() -> None:
    live = session([1])
    live.start()
    finished = live.advance([DecisionActions(1, 0, [])])
    assert isinstance(finished, Finished)
    [rollout] = finished.rollouts
    assert rollout.stop == UnpaidClaims(month=0, claims=[ClaimId(month=0, index=0)])
    assert rollout.trace is not None
    assert rollout.trace.receipts == []
    assert rollout.trace.events.obligation_settlements.get_column("amount_paid_quanta").to_list() == [0]


@pytest.mark.parametrize("keys", [[], [(0, 0)], [(0, 0), (0, 0)], [(0, 1), (1, 0)], [(0, 0), (2, 0)]])
def test_bad_routing_aborts_without_resubmission(keys: list[tuple[int, int]]) -> None:
    live = session([0, 1])
    live.start()
    with pytest.raises(ValueError, match="each active path/month"):
        live.advance([DecisionActions(id_, month, []) for id_, month in keys])
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        live.advance([DecisionActions(0, 0, []), DecisionActions(1, 0, [])])


def test_cross_rollout_claim_handle_is_a_routing_error() -> None:
    live = session([0, 1])
    batch = live.start()
    assert not isinstance(batch, Finished)
    claim = batch[0].observation.claims[0]
    with pytest.raises(ValueError, match="different rollout"):
        live.advance(
            [
                DecisionActions(
                    id_,
                    0,
                    [PayClaim(request_id=0, cause_id="pay", claim=claim, from_account=claim.from_account, amount=1)],
                )
                for id_ in [0, 1]
            ]
        )
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        live.start()


def test_claim_handle_cannot_alias_another_sessions_claim() -> None:
    first = session([0])
    first_batch = first.start()
    assert not isinstance(first_batch, Finished)
    old_claim = first_batch[0].observation.claims[0]
    first.close()
    second = ActionSession({0: compose(0, obligation_id="different-bill")}, "alice")
    second_batch = second.start()
    assert not isinstance(second_batch, Finished)
    assert second_batch[0].observation.claims[0].cause_id != old_claim.cause_id
    with pytest.raises(ValueError, match="different rollout or session"):
        second.advance(
            [
                DecisionActions(
                    0,
                    0,
                    [
                        PayClaim(
                            request_id=0, cause_id="pay", claim=old_claim, from_account=old_claim.from_account, amount=1
                        )
                    ],
                )
            ]
        )
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        second.advance([])


def test_repeated_start_and_advance_before_start_abort() -> None:
    early = session([0])
    with pytest.raises(ValueError, match="lifecycle state"):
        early.advance([])
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        early.start()
    repeated = session([0])
    repeated.start()
    with pytest.raises(ValueError, match="lifecycle state"):
        repeated.start()
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        repeated.advance([])


def test_copied_observations_do_not_mutate_books() -> None:
    live = session([0])
    batch = live.start()
    assert not isinstance(batch, Finished)
    observation = batch[0].observation
    copied_accounts: Any = observation.accounts
    with pytest.raises(TypeError, match="does not support item assignment"):
        copied_accounts[0] = ("invented", 999)
    writable: Any = observation
    with pytest.raises(ValueError, match="frozen"):
        writable.cash = 999
    claim = observation.claims[0]
    batch = live.advance(
        [
            DecisionActions(
                0, 0, [PayClaim(request_id=0, cause_id="pay", claim=claim, from_account=claim.from_account, amount=1)]
            )
        ]
    )
    assert not isinstance(batch, Finished)
    assert batch[0].observation.accounts == (("checking", 6),)
    live.close()


@pytest.mark.parametrize(
    ("ids", "message"), [([], "a session needs at least one world"), ([ROLLOUT_COUNT], "invalid rollout selection")]
)
def test_invalid_selection_rejects_at_construction(ids: list[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        session(ids)


def test_invalid_capture_rejects_at_construction() -> None:
    invalid: Any = "invented"
    with pytest.raises(ValueError, match="capture must be"):
        session([0], capture=invalid)


def test_extraction_error_and_explicit_close_release_the_session() -> None:
    live = session([0])
    live.start()
    invalid: Any = [None]
    with pytest.raises(TypeError):
        live.advance(invalid)
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        live.start()
    closed = session([0])
    closed.close()
    closed.close()
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        closed.start()


if __name__ == "__main__":
    pytest_bazel.main()
