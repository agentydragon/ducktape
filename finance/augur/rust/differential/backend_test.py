"""The backend layer itself: both engines answer in one schema, and disagreement is loud."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import (
    BACKENDS,
    CANONICAL_STATE_CHANNELS,
    Backend,
    assert_backends_agree,
    assert_results_agree,
    run_jax,
    run_rust,
)
from finance.augur.rust.differential.fixtures import shared_case


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_answers_in_the_canonical_schema(backend: Backend) -> None:
    """The point of the layer: a caller reads a result without knowing which engine ran.

    The shared case opens balances, holds lots and sells one, so a backend that answered
    with an empty channel would be failing to report state it certainly has.
    """

    channels = backend(shared_case()).state_channels
    assert set(channels) == set(CANONICAL_STATE_CHANNELS)
    for name in ("cash", "lots", "rollout_status"):
        assert not channels[name].is_empty(), f"{name} is empty"
    assert set(channels["cash"].columns) == {"rollout_index", "month_index", "agent_id", "account_id", "balance_quanta"}


def test_backends_agree_on_the_shared_case() -> None:
    assert_backends_agree(shared_case())


def test_disagreement_names_the_channel_that_differs() -> None:
    """A comparison that silently passed would make every suite built on it meaningless."""

    with pytest.raises(AssertionError, match="state channel 'cash' differs"):
        assert_results_agree(run_jax(shared_case()), run_rust(shared_case(alice_opening=Decimal("10.01"))))


if __name__ == "__main__":
    pytest_bazel.main()
