"""Real native handoffs preserve exact money, rejected prefixes, and file replay identity."""

import json
from typing import Literal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.rust.simulator import ActionSession
from finance.augur.sim.results import Finished, Paid, PaymentReceipt, PaymentRejected, RejectedAction
from finance.augur.x.monthly_actions.policy import decide
from finance.augur.x.monthly_actions.run import prepare


@pytest.mark.parametrize("capture", ["summary", "forensic"])
def test_native_results_and_file_replay_keep_exact_successful_prefix(capture: Literal["summary", "forensic"]) -> None:
    session = ActionSession(json.dumps(prepare().execution_input), "example-household", [1, 0], capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(decide(batch))
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
    finally:
        session.close()


def test_fractional_money_cannot_enter_a_typed_payment_result() -> None:
    # Native Money is integer quanta; a file with fractional quanta must not be rounded/coerced.
    with pytest.raises(ValidationError):
        PaymentReceipt.model_validate_json(
            '{"request_id":0,"target":{"kind":"Consumption","component_id":"test"},'
            '"amount_requested":1.5,"outcome":{"kind":"Paid"}}'
        )


if __name__ == "__main__":
    pytest_bazel.main()
