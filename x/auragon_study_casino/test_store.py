"""SqlStore: state-dump, idempotent server actions, snapshot semantics."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from x.auragon_study_casino.models import BalanceRow, StateSnapshotRow
from x.auragon_study_casino.store import ActionMutation, ActionRejectedError, ServerActionResult, SqlStore


@pytest.fixture
def store(tmp_path: Path) -> SqlStore:
    return SqlStore(tmp_path / "casino.db")


def test_fresh_store_has_zero_balance_and_default_prizes(store: SqlStore) -> None:
    state = store.state_dump()
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6  # DEFAULT_PRIZES


def test_run_server_action_persists_balance_and_writes_ledger(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits += 7
        return ActionMutation(result={"granted": 7}, details={"reason": "test"})

    result = store.run_server_action(client_action_id="act-1", action_type="test.grant", mutator=grant_credits)
    assert isinstance(result, ServerActionResult)
    assert result.event.action_type == "test.grant"
    assert result.event.credits_before == 0
    assert result.event.credits_after == 7
    assert result.result == {"granted": 7}

    state = store.state_dump()
    assert state["balance"]["credits"] == 7

    ledger = store.list_ledger_events()
    assert len(ledger) == 1
    assert ledger[0].client_action_id == "act-1"


def test_run_server_action_is_idempotent_on_retry(store: SqlStore) -> None:
    def grant_credits(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits += 5
        return ActionMutation(result={"granted": 5})

    first = store.run_server_action(client_action_id="act-dup", action_type="test.grant", mutator=grant_credits)
    second = store.run_server_action(client_action_id="act-dup", action_type="test.grant", mutator=grant_credits)
    assert second.event.id == first.event.id
    assert second.result == first.result
    # Mutator was NOT replayed — credits stayed at 5, not 10.
    assert store.state_dump()["balance"]["credits"] == 5


def test_action_rejected_rolls_back_mutator_changes(store: SqlStore) -> None:
    def half_then_reject(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits += 99
        raise ActionRejectedError("nope", "no good")

    with pytest.raises(ActionRejectedError):
        store.run_server_action(client_action_id="act-reject", action_type="test.reject", mutator=half_then_reject)

    # Transaction was rolled back: balance unchanged, no ledger row.
    assert store.state_dump()["balance"]["credits"] == 0
    assert len(store.list_ledger_events()) == 0


def test_snapshot_reason_writes_state_snapshots_row(store: SqlStore) -> None:
    def import_payload(s, _now_ms):
        store.replace_state_for_import(
            s,
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
        client_action_id="act-import",
        action_type="data.import",
        mutator=import_payload,
        snapshot_reason="before_import",
        snapshot_note="unit test",
    )

    state = store.state_dump()
    assert state["balance"] == {"credits": 11, "tokens": 22}
    assert [p["id"] for p in state["prizes"]] == ["p-only"]

    with store._Session() as s:
        snapshots = list(s.scalars(select(StateSnapshotRow).order_by(StateSnapshotRow.id)).all())
    assert len(snapshots) == 1
    assert snapshots[0].reason == "before_import"
    assert snapshots[0].note == "unit test"


def test_replace_state_for_reset_keeps_prizes(store: SqlStore) -> None:
    def grant_then_reset(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits = 50
        balance.tokens = 30
        return ActionMutation(result={"granted": True})

    store.run_server_action(client_action_id="act-grant", action_type="test.grant", mutator=grant_then_reset)
    pre_reset = store.state_dump()
    assert pre_reset["balance"] == {"credits": 50, "tokens": 30}

    def reset(s, _now_ms):
        store.replace_state_for_reset(s)
        return ActionMutation(result={"reset": True})

    store.run_server_action(
        client_action_id="act-reset", action_type="data.reset", mutator=reset, snapshot_reason="before_reset"
    )

    state = store.state_dump()
    assert state["balance"] == {"credits": 0, "tokens": 0}
    assert state["sessions"] == []
    assert state["prize_log"] == []
    assert len(state["prizes"]) == 6  # default catalog preserved


def test_db_check_constraint_rejects_negative_credits(store: SqlStore) -> None:
    """The CHECK constraint is the last line of defence if a buggy mutator
    misses a pre-flight check."""

    def goes_negative(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits = -1
        return ActionMutation(result={"oops": True})

    with pytest.raises(IntegrityError):
        store.run_server_action(client_action_id="act-bug", action_type="test.bug", mutator=goes_negative)


def test_state_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "casino.db"
    store_a = SqlStore(db)

    def grant(s, _now_ms):
        balance = s.scalar(select(BalanceRow).where(BalanceRow.id == 1).with_for_update())
        balance.credits = 13
        return ActionMutation(result={"granted": True})

    store_a.run_server_action(client_action_id="reopen-1", action_type="test.grant", mutator=grant)

    store_b = SqlStore(db)
    assert store_b.state_dump()["balance"]["credits"] == 13


if __name__ == "__main__":
    pytest_bazel.main()
