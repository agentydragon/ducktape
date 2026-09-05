"""Comparing the two engines, once each has projected its run into the shared shape.

The shape and each engine's projection into it live elsewhere — `sim/testing/simulation_result.py`
for the declaration and JAX's side, `rust/simulation_result.py` for Rust's. What is left here is
the comparison itself, which is the only part that is about there being exactly two engines.

Channels only one engine has stay visibly one engine's: the balanced journal, the TLH deferral
ledger and held bond principal are Rust-only, and a test wanting them asks the Rust result for
them by name.
"""

import polars as pl
from polars.testing import assert_frame_equal

from finance.augur.rust.result import RustResult, run_rust
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.jax_result import run_jax
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

BACKENDS: tuple[Backend, ...] = (run_jax, run_rust)


def _failure_months(status: pl.DataFrame) -> pl.DataFrame:
    """`(rollout_index, month_index)` for each rollout that ran out of cash.

    Read from `rollout_status`, which is itself compared between the engines and agrees — so
    the rows this excludes below are identified by something the two engines concur on, not
    by one engine's account of where it stopped.
    """

    return (
        status.filter(pl.col("failed_month").is_not_null())
        .select("rollout_index", pl.col("failed_month").alias("month_index"))
        .cast({"month_index": pl.Int64})
    )


def _outside_the_failure_month(frame: pl.DataFrame, failure_months: pl.DataFrame) -> pl.DataFrame:
    """Drop what a rollout recorded during the month it could not pay.

    The engines do not agree about that one month and the question is open: Rust stops inside
    its month loop at the phase that could not pay, so whether a phase was recorded depends on
    where it sits in that order, while JAX cannot leave a vectorized scan partway through a
    month and reports the whole month or none of it. No month-level rule reproduces an ordering
    *within* one, so this is a modelling decision nobody has made rather than a defect either
    engine can be said to have.

    Both answers stay pinned in `known_divergence_test.py`, which is what keeps this narrow: a
    change to either engine's behaviour in the failure month still fails there and has to state
    its intent. What is given up is the fuzzer's chance of finding an *unrelated* bug that
    happens to land in the failure month of a failed rollout — one month of the rollouts that
    fail at all. Every other month of every rollout still compares in full.
    """

    if not {"rollout_index", "month_index"} <= set(frame.columns) or failure_months.is_empty():
        return frame
    return frame.join(failure_months, on=["rollout_index", "month_index"], how="anti")


def assert_results_agree(expected: SimulationResult, actual: SimulationResult) -> None:
    """Every channel both engines answer in, plus every canonical event frame."""

    for name, frame in expected.state_channels.items():
        try:
            assert_frame_equal(actual.state_channels[name], frame, check_row_order=False, check_column_order=False)
        except AssertionError as error:
            raise AssertionError(f"state channel {name!r} differs between backends") from error
    failure_months = _failure_months(expected.rollout_status)
    for spec in EVENT_FRAME_SPECS:
        try:
            assert_frame_equal(
                _outside_the_failure_month(actual.events.frame(spec), failure_months),
                _outside_the_failure_month(expected.events.frame(spec), failure_months),
                check_row_order=False,
            )
        except AssertionError as error:
            raise AssertionError(f"event frame {spec.name!r} differs between backends") from error


def assert_backends_agree(case: Case) -> RustResult:
    """Run the case on both engines and return the Rust result.

    Returning the Rust one is not a preference: it carries the same values in every shared
    channel by the time this returns, plus the journal and ledgers JAX has no counterpart
    for, so a caller needing those does not run the case twice.
    """

    jax_result, rust = run_jax(case), run_rust(case)
    assert_results_agree(jax_result, rust)
    return rust
