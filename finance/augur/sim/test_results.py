"""Typed results preserve exact money, rejected prefixes, and file replay identity."""

from typing import Literal

import pytest
import pytest_bazel

from finance.augur.sim.results import Finished, Paid, PaymentRejected, RejectedAction
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.funded_bill import HOUSEHOLD, compose, sell_then_pay, situation
from finance.augur.sim.testing.session import finish


@pytest.mark.parametrize("capture", ["summary", "forensic"])
def test_results_and_file_replay_keep_exact_successful_prefix(capture: Literal["summary", "forensic"]) -> None:
    case = situation()
    batch = finish(
        ActionSession({id_: compose(case, id_) for id_ in (1, 0)}, HOUSEHOLD, capture=capture), sell_then_pay
    )
    stopped, completed = batch.rollouts
    assert [row.rollout_id for row in batch.rollouts] == [1, 0]
    assert stopped.stop == RejectedAction(month=0, action_index=1)
    assert stopped.summary.ending_book.month == 1
    assert stopped.summary.cash[0].values == [0, 10_000]
    assert isinstance(stopped.summary.payments[0].receipt.outcome, PaymentRejected)
    assert stopped.summary.payments[0].receipt.amount_paid == 0
    assert completed.stop is None
    assert completed.summary.cash[0].values[-1] == 3_800
    assert all(isinstance(row.receipt.outcome, Paid) for row in completed.summary.payments)
    assert batch == Finished.model_validate_json(batch.model_dump_json())
    if stopped.trace is not None:
        assert stopped.trace.events.rollout_ids == (1,)
        assert stopped.trace.books[-1] == stopped.summary.ending_book
        for entry in stopped.trace.journal:
            assert sum(posting.amount for posting in entry.postings) == 0


if __name__ == "__main__":
    pytest_bazel.main()
