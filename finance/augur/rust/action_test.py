"""Exercise real Python-owned batches, routing errors, receipt feedback and fatal stops."""

import json
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel

from finance.augur.rust.simulator import Action, ActionSession, DecisionActions
from finance.augur.sim.results import ClaimId, Consume, Executed, Finished, RejectedAction, Rollout, UnpaidClaims
from finance.augur.sim.scenario import ObligationType, ScheduledObligation, ScheduledTransfer
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking


@pytest.fixture
def input_json() -> str:
    case = Case(
        scenario(
            checking(("alice", Decimal("0.05")), ("world", Decimal(0))),
            horizon_months=4,
            tax_profiles=[],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=0,
                    cause_id="opening-contribution",
                    from_agent_id="world",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal("0.02"),
                )
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=0,
                    obligation_id="one-cent-bill",
                    obligation_type=ObligationType.OUTSIDE_RENT,
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="world",
                    to_account_id="checking",
                    amount_due=Decimal("0.01"),
                )
            ],
        ),
        rollout_count=2,
    )
    return json.dumps(case.compiled_run.execution_input)


def consume(amount: int, cause: str = "chosen-spend") -> Action:
    return Action.consume(0, cause, "budget", ("alice", "checking"), ("world", "checking"), amount)


def run(input_json: str, ids: list[int]) -> tuple[list[Rollout], dict[int, list[tuple[int, int]]]]:
    session = ActionSession(input_json, "alice", ids)
    observed: dict[int, list[tuple[int, int]]] = {id_: [] for id_ in ids}
    memory = dict.fromkeys(ids, 0)
    try:
        batch = session.start()
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
                assert observation.accounts == [("checking", observation.cash)]
                assert observation.agent_id == "alice"
                assert observation.cpi is None  # This nominal-only experiment supplied no CPI model.
                assert not observation.public_positions
                actions = [
                    Action.pay_claim(i, "pay-bill", claim, claim.from_account, claim.amount_due)
                    for i, claim in enumerate(observation.claims)
                ]
                actions.append(consume(1))
                responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
            batch = session.advance(responses)
        return batch.rollouts, observed
    finally:
        session.close()


def test_current_facts_receipt_memory_and_original_replay(input_json: str) -> None:
    baseline, observed = run(input_json, [0, 1])
    assert observed == {id_: [(0, 7), (1, 5), (2, 4), (3, 3)] for id_ in [0, 1]}
    for ids in [[1, 0], [1], [0], [0, 1]]:
        actual, replay_observed = run(input_json, ids)
        assert actual == [baseline[id_] for id_ in ids]
        assert replay_observed == {id_: observed[id_] for id_ in ids}
    assert [row.rollout_id for row in baseline] == [0, 1]
    assert all(row.stop is None for row in baseline)


def test_action_order_prefix_retention_and_independent_continuation(input_json: str) -> None:
    session = ActionSession(input_json, "alice", [0, 1])
    batch = session.start()
    assert not isinstance(batch, Finished)
    responses = []
    for decision in batch:
        claim = decision.observation.claims[0]
        actions = [Action.pay_claim(0, "pay", claim, claim.from_account, claim.amount_due)]
        if decision.rollout_id == 0:
            actions += [
                Action.transfer("prefix", ("alice", "checking"), ("world", "checking"), 1),
                consume(100),
                consume(1, "unattempted-suffix"),
            ]
        responses.append(DecisionActions(decision.rollout_id, 0, actions))
    batch = session.advance(responses)
    for month in range(1, 4):
        assert not isinstance(batch, Finished)
        assert [decision.rollout_id for decision in batch] == [1]
        assert batch[0].observation.month == month
        batch = session.advance([DecisionActions(1, month, [])])
    assert isinstance(batch, Finished)
    stopped, completed = batch.rollouts
    assert stopped.stop == RejectedAction(month=0, action_index=2)
    assert stopped.trace is not None
    assert [row.action.kind for row in stopped.trace.receipts] == ["PayClaim", "Transfer", "Consume"]
    closing = stopped.trace.books[-1]
    assert next(row.balance for row in closing.balances if row.account.agent_id == "alice") == 5
    assert completed.stop is None
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        session.advance([])


def test_unpaid_due_claim_is_not_an_implicit_payment(input_json: str) -> None:
    session = ActionSession(input_json, "alice", [1])
    session.start()
    finished = session.advance([DecisionActions(1, 0, [])])
    assert isinstance(finished, Finished)
    [rollout] = finished.rollouts
    assert rollout.stop == UnpaidClaims(month=0, claims=[ClaimId(month=0, index=0)])
    assert rollout.trace is not None
    assert rollout.trace.receipts == []
    assert rollout.trace.events.obligation_settlements.get_column("amount_paid_quanta").to_list() == [0]


@pytest.mark.parametrize("keys", [[], [(0, 0)], [(0, 0), (0, 0)], [(0, 1), (1, 0)], [(0, 0), (2, 0)]])
def test_bad_routing_aborts_without_resubmission(input_json: str, keys: list[tuple[int, int]]) -> None:
    session = ActionSession(input_json, "alice", [0, 1])
    session.start()
    with pytest.raises(ValueError, match="each active path/month"):
        session.advance([DecisionActions(id_, month, []) for id_, month in keys])
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        session.advance([DecisionActions(0, 0, []), DecisionActions(1, 0, [])])


def test_cross_rollout_claim_handle_is_a_routing_error(input_json: str) -> None:
    session = ActionSession(input_json, "alice", [0, 1])
    batch = session.start()
    assert not isinstance(batch, Finished)
    claim = batch[0].observation.claims[0]
    with pytest.raises(ValueError, match="different rollout"):
        session.advance(
            [DecisionActions(id_, 0, [Action.pay_claim(0, "pay", claim, claim.from_account, 1)]) for id_ in [0, 1]]
        )
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        session.start()


def test_claim_handle_cannot_alias_another_sessions_claim(input_json: str) -> None:
    first = ActionSession(input_json, "alice", [0])
    first_batch = first.start()
    assert not isinstance(first_batch, Finished)
    old_claim = first_batch[0].observation.claims[0]
    first.close()
    second = ActionSession(input_json.replace("one-cent-bill", "different-bill"), "alice", [0])
    second_batch = second.start()
    assert not isinstance(second_batch, Finished)
    assert second_batch[0].observation.claims[0].cause_id != old_claim.cause_id
    with pytest.raises(ValueError, match="different rollout or session"):
        second.advance([DecisionActions(0, 0, [Action.pay_claim(0, "pay", old_claim, old_claim.from_account, 1)])])
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        second.advance([])


def test_repeated_start_and_advance_before_start_abort(input_json: str) -> None:
    early = ActionSession(input_json, "alice", [0])
    with pytest.raises(ValueError, match="lifecycle state"):
        early.advance([])
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        early.start()
    repeated = ActionSession(input_json, "alice", [0])
    repeated.start()
    with pytest.raises(ValueError, match="lifecycle state"):
        repeated.start()
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        repeated.advance([])


def test_copied_observations_do_not_mutate_books(input_json: str) -> None:
    session = ActionSession(input_json, "alice", [0])
    batch = session.start()
    assert not isinstance(batch, Finished)
    observation = batch[0].observation
    copied_accounts = observation.accounts
    copied_accounts[0] = ("invented", 999)
    writable: Any = observation
    with pytest.raises(AttributeError):
        writable.cash = 999
    claim = observation.claims[0]
    batch = session.advance([DecisionActions(0, 0, [Action.pay_claim(0, "pay", claim, claim.from_account, 1)])])
    assert not isinstance(batch, Finished)
    assert batch[0].observation.accounts == [("checking", 6)]
    session.close()


@pytest.mark.parametrize("ids", [[], [0, 0], [2]])
def test_invalid_selection_rejects_at_construction(input_json: str, ids: list[int]) -> None:
    with pytest.raises(ValueError, match="selected rollout IDs"):
        ActionSession(input_json, "alice", ids)


def test_invalid_capture_rejects_at_construction(input_json: str) -> None:
    invalid: Any = "invented"
    with pytest.raises(ValueError, match="capture must be"):
        ActionSession(input_json, "alice", [0], capture=invalid)


def test_extraction_error_and_explicit_close_release_the_session(input_json: str) -> None:
    session = ActionSession(input_json, "alice", [0])
    session.start()
    invalid: Any = [None]
    with pytest.raises(TypeError):
        session.advance(invalid)
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        session.start()
    closed = ActionSession(input_json, "alice", [0])
    closed.close()
    closed.close()
    with pytest.raises(ValueError, match="finished, aborted or closed"):
        closed.start()


if __name__ == "__main__":
    pytest_bazel.main()
