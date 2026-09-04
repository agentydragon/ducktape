"""The backend layer itself: both engines answer in one schema, and disagreement is loud."""

import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import (
    BACKENDS,
    CANONICAL_STATE_CHANNELS,
    KNOWN_DIVERGENT_COLUMNS,
    SimulationResult,
    assert_backends_agree,
    assert_results_agree,
    run_jax,
    run_rust,
)
from finance.augur.rust.differential.fixtures import property_depreciation_fixture, shared_integer_fixture


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_answers_in_the_canonical_schema(backend) -> None:
    """The point of the layer: a caller reads a result without knowing which engine ran.

    The shared fixture opens balances, holds lots and sells one, so a backend that answered
    with an empty channel would be failing to report state it certainly has.
    """

    channels = backend(shared_integer_fixture()).state_channels
    assert set(channels) == set(CANONICAL_STATE_CHANNELS)
    for name in ("cash", "lots", "rollout_status"):
        assert not channels[name].is_empty(), f"{name} is empty"
    assert set(channels["cash"].columns) == {"rollout_index", "month_index", "agent_id", "account_id", "balance_quanta"}


def test_backends_agree_on_the_shared_fixture() -> None:
    assert_backends_agree(shared_integer_fixture())


def test_disagreement_names_the_channel_that_differs() -> None:
    """A comparison that silently passed would make every suite built on it meaningless."""

    fixture = shared_integer_fixture()
    accounts = fixture["scenario"]["accounts"]
    perturbed = {
        **fixture,
        "scenario": {
            **fixture["scenario"],
            "accounts": [{**accounts[0], "opening_balance": accounts[0]["opening_balance"] + 1}, *accounts[1:]],
        },
    }
    with pytest.raises(AssertionError, match="state channel 'cash' differs"):
        assert_results_agree(run_jax(fixture), run_rust(perturbed))


def test_the_known_ytd_principal_divergence_is_still_real() -> None:
    """Pins the one column `assert_results_agree` excludes.

    JAX never resets `liability_principal_ytd` at the tax year while Rust does, so the two
    part company in month 12. When JAX gains the missing reset this test fails, which is the
    signal to drop the entry from `KNOWN_DIVERGENT_COLUMNS` and compare the column again.
    """

    assert KNOWN_DIVERGENT_COLUMNS["liabilities"] == ("principal_paid_ytd_quanta",)
    fixture = property_depreciation_fixture(sale=False)
    jax_result, rust = run_jax(fixture), run_rust(fixture)

    def paid(result: SimulationResult) -> list[int]:
        column = result.liabilities.sort("month_index").get_column("principal_paid_ytd_quanta")
        return [int(value) for value in column]

    jax_paid, rust_paid = paid(jax_result), paid(rust)
    # The reset lands on the fixture's tax-year-end month, whichever that is.
    reset = next(month for month, value in enumerate(rust_paid) if month > 0 and value == 0)
    assert jax_paid[:reset] == rust_paid[:reset], "the engines agree within the first tax year"
    assert rust_paid[reset] == 0, "Rust resets the accumulator at the year boundary"
    assert jax_paid[reset] > jax_paid[reset - 1], "JAX keeps accumulating through it"


if __name__ == "__main__":
    pytest_bazel.main()
