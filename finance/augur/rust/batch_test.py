"""Exercise the prototype extension's real monthly boundary, routing and terminal state."""

import json
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.simulator import PrototypeSpendingSession
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import checking


def session(ids: list[int], *, forensic: bool = False) -> PrototypeSpendingSession:
    case = Case(
        scenario(checking(("alice", Decimal("0.05")), ("world", Decimal(0))), horizon_months=4, tax_profiles=[]),
        rollout_count=2,
        series={InflationKey(): flat(Decimal(1), rollout_count=2, horizon_months=4)},
    )
    return PrototypeSpendingSession(
        json.dumps(case.compiled_run.execution_input),
        json.dumps(
            {
                "from": {"agent_id": "alice", "account_id": "checking"},
                "to": {"agent_id": "world", "account_id": "checking"},
                "cause_id": "consumption",
            }
        ),
        ids,
        forensic=forensic,
    )


@pytest.mark.parametrize("ids", [[0, 1], [1, 0], [0], [1]])
@pytest.mark.parametrize("chunked", [False, True])
def test_path_local_memory_and_selected_replay(ids: list[int], chunked: bool) -> None:
    native = session(ids)
    memory = dict.fromkeys(ids, 0)
    observations: dict[int, list[tuple[int, int]]] = {id_: [] for id_ in ids}
    while (batch := native.observe()).rollout_ids:
        requests = []
        for id_, month, cash, public in zip(
            batch.rollout_ids, batch.months, batch.cash, batch.public_holdings, strict=True
        ):
            observations[id_].append((month, cash))
            assert public == 0
            memory[id_] += 1
            requests.append((id_, month, memory[id_] if id_ == 0 else 0))
        requests.reverse()
        if chunked:
            for request in requests:
                native.advance([request])
        else:
            native.advance(requests)
    output = json.loads(native.finish_json())
    assert observations.get(0, []) == ([(0, 5), (1, 4), (2, 2)] if 0 in ids else [])
    assert observations.get(1, []) == ([(month, 5) for month in range(4)] if 1 in ids else [])
    assert output["consumption_requested"] == [[1, 2, 3] if id_ == 0 else [0] * 4 for id_ in ids]
    assert output["consumption_paid"] == [[1, 2, 0] if id_ == 0 else [0] * 4 for id_ in ids]
    assert output["product_metrics"]["failed_month"] == [2 if id_ == 0 else -1 for id_ in ids]
    with pytest.raises(ValueError, match="finished or aborted"):
        native.observe()


def test_forensic_replay_has_original_id_and_actual_paid_receipts() -> None:
    native = session([1], forensic=True)
    for month in range(4):
        assert native.observe().rollout_ids == [1]
        native.advance([(1, month, 1)])
    output = json.loads(native.finish_json())
    assert output["rollouts"][0]["rollout_id"] == 1
    assert [row["amount_paid"] for row in output["rollouts"][0]["obligations"]] == [1] * 4
    assert [row["rollout_index"] for row in output["event_frames"]["obligation_settlements"]] == [1] * 4


@pytest.mark.parametrize(
    "requests", [[(0, 0, -1)], [(0, 1, 0)], [(3, 0, 0)], [(0, 0, 1), (0, 0, 2)], [(0, 0, 1 << 63)], [(0, 0, 1.5)]]
)
def test_bad_requests_abort_without_resubmission(requests: Any) -> None:
    native = session([0, 1])
    with pytest.raises((ValueError, OverflowError, TypeError)):
        native.advance(requests)
    with pytest.raises(ValueError, match="finished or aborted"):
        native.advance([(0, 0, 0)])


def test_early_finish_and_stale_requests_are_not_resumable() -> None:
    early = session([0])
    with pytest.raises(ValueError, match="remain live"):
        early.finish_json()
    with pytest.raises(ValueError, match="finished or aborted"):
        early.observe()
    stale = session([0])
    stale.advance([(0, 0, 1)])
    with pytest.raises(ValueError, match="current month"):
        stale.advance([(0, 0, 1)])
    stopped = session([0, 1])
    stopped.advance([(0, 0, 6)])
    assert stopped.observe().rollout_ids == [1]
    with pytest.raises(ValueError, match="current month"):
        stopped.advance([(0, 1, 0)])


if __name__ == "__main__":
    pytest_bazel.main()
