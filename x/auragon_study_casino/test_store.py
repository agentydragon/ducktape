"""SqlStore: state-dump, idempotent server actions, snapshot semantics, tenant isolation."""

from __future__ import annotations

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from x.auragon_study_casino.models import BalanceRow, StateSnapshotRow
from x.auragon_study_casino.store import ActionMutation, ActionRejectedError, ServerActionResult, SqlStore


@pytest.fixture
def store(db_url: str) -> SqlStore:
    return SqlStore(db_url)


# Single-tenant tests use "u" as the canonical test user.
_U = "u"


def test_fresh_store_has_zero_balance_and_default_prizes(store: SqlStore) -> None:
    state = store.state_dump(_U)
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6  # DEFAULT_PRIZES


def test_run_server_action_persists_balance_and_writes_ledger(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits += 7
        return ActionMutation(result={"granted": 7}, details={"reason": "test"})

    result = store.run_server_action(
        username=_U, client_action_id="act-1", action_type="test.grant", mutator=grant_credits
    )
    assert isinstance(result, ServerActionResult)
    assert result.event.action_type == "test.grant"
    assert result.event.credits_before == 0
    assert result.event.credits_after == 7
    assert result.result == {"granted": 7}

    state = store.state_dump(_U)
    assert state["balance"]["credits"] == 7

    ledger = store.list_ledger_events(_U)
    assert len(ledger) == 1
    assert ledger[0].client_action_id == "act-1"


def test_run_server_action_is_idempotent_on_retry(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits += 5
        return ActionMutation(result={"granted": 5})

    first = store.run_server_action(
        username=_U, client_action_id="act-dup", action_type="test.grant", mutator=grant_credits
    )
    second = store.run_server_action(
        username=_U, client_action_id="act-dup", action_type="test.grant", mutator=grant_credits
    )
    assert second.event.id == first.event.id
    assert second.result == first.result
    # Mutator was NOT replayed — credits stayed at 5, not 10.
    assert store.state_dump(_U)["balance"]["credits"] == 5


def test_action_rejected_rolls_back_mutator_changes(store: SqlStore) -> None:
    def half_then_reject(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits += 99
        raise ActionRejectedError("nope", "no good")

    with pytest.raises(ActionRejectedError):
        store.run_server_action(
            username=_U, client_action_id="act-reject", action_type="test.reject", mutator=half_then_reject
        )

    # Transaction was rolled back: balance unchanged, no ledger row.
    assert store.state_dump(_U)["balance"]["credits"] == 0
    assert len(store.list_ledger_events(_U)) == 0


def test_snapshot_reason_writes_state_snapshots_row(store: SqlStore) -> None:
    def import_payload(s, _now_ms):
        store.replace_state_for_import(
            s,
            _U,
            {
                "credits": 11,
                "tokens": 22,
                "sessions": [],
                "prizes": [{"id": "p-only", "name": "Only", "cost": 5}],
                "prize_log": [],
            },
        )
        return ActionMutation(result={"imported": True})

    store.run_server_action(
        username=_U,
        client_action_id="act-import",
        action_type="data.import",
        mutator=import_payload,
        snapshot_reason="before_import",
        snapshot_note="unit test",
    )

    state = store.state_dump(_U)
    assert state["balance"] == {"credits": 11, "tokens": 22}
    assert [p["id"] for p in state["prizes"]] == ["p-only"]

    with store._Session() as s:
        snapshots = list(
            s.scalars(
                select(StateSnapshotRow).where(StateSnapshotRow.user_id == _U).order_by(StateSnapshotRow.id)
            ).all()
        )
    assert len(snapshots) == 1
    assert snapshots[0].reason == "before_import"
    assert snapshots[0].note == "unit test"


def test_replace_state_for_reset_keeps_prizes(store: SqlStore) -> None:
    def grant_then_reset(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits = 50
        balance.tokens = 30
        return ActionMutation(result={"granted": True})

    store.run_server_action(
        username=_U, client_action_id="act-grant", action_type="test.grant", mutator=grant_then_reset
    )
    pre_reset = store.state_dump(_U)
    assert pre_reset["balance"] == {"credits": 50, "tokens": 30}

    def reset(s, _now_ms):
        store.replace_state_for_reset(s, _U)
        return ActionMutation(result={"reset": True})

    store.run_server_action(
        username=_U,
        client_action_id="act-reset",
        action_type="data.reset",
        mutator=reset,
        snapshot_reason="before_reset",
    )

    state = store.state_dump(_U)
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6  # default catalog preserved


def test_db_check_constraint_rejects_negative_credits(store: SqlStore) -> None:
    """The CHECK constraint is the last line of defence if a buggy mutator misses a pre-flight check."""

    def goes_negative(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits = -1
        return ActionMutation(result={"oops": True})

    with pytest.raises(IntegrityError):
        store.run_server_action(username=_U, client_action_id="act-bug", action_type="test.bug", mutator=goes_negative)


def test_state_persists_across_reopen(db_url: str) -> None:
    store_a = SqlStore(db_url)

    def grant(s, _now_ms):
        balance = next(iter(s.execute(_balance_select(_U)).scalars()))
        balance.credits = 13
        return ActionMutation(result={"granted": True})

    store_a.run_server_action(username=_U, client_action_id="reopen-1", action_type="test.grant", mutator=grant)

    store_b = SqlStore(db_url)
    assert store_b.state_dump(_U)["balance"]["credits"] == 13


def test_two_users_share_db_without_collision(store: SqlStore) -> None:
    """Two users on the same store have independent balances, sessions, ledgers."""

    def grant_alice(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.user_id == "alice").with_for_update())
        assert balance is not None
        balance.credits += 10
        return ActionMutation(result={"alice": 10})

    def grant_bob(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.user_id == "bob").with_for_update())
        assert balance is not None
        balance.credits += 99
        return ActionMutation(result={"bob": 99})

    # Same client_action_id used for both — must NOT collide since the
    # unique constraint is (user_id, client_action_id).
    store.run_server_action(username="alice", client_action_id="dup-id", action_type="t", mutator=grant_alice)
    store.run_server_action(username="bob", client_action_id="dup-id", action_type="t", mutator=grant_bob)

    assert store.state_dump("alice")["balance"]["credits"] == 10
    assert store.state_dump("bob")["balance"]["credits"] == 99
    # Each user's ledger lists only their own row.
    assert len(store.list_ledger_events("alice")) == 1
    assert len(store.list_ledger_events("bob")) == 1


def _balance_select(username: str):
    """Return a select() for the singleton BalanceRow of `username`, locked for update."""
    return select(BalanceRow).where(BalanceRow.user_id == username).with_for_update()


if __name__ == "__main__":
    pytest_bazel.main()
