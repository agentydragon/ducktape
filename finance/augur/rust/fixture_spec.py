"""Writing the integer fixture the Rust simulator consumes.

`fixture.rs` defines this format with `deny_unknown_fields`, so a fixture is authored as
plain JSON-shaped dicts and a misspelled key is rejected at the boundary rather than
silently ignored. These helpers name the shapes that recur, so a fixture reads as the
scenario it describes instead of as nested punctuation.

`series` is the one that earns its place beyond brevity: a series' values cross the
boundary as one flat row-major block, and a hand-written list of the wrong length or with
rollouts transposed is still a *valid* fixture describing a different scenario. Supplying
`value_at(rollout, month)` makes that unrepresentable.
"""

from collections.abc import Callable
from typing import Any

from more_itertools import one

# The fixture schema `fixture.rs` accepts. Bumped there and here together.
SCHEMA_VERSION = 8


def account_ref(agent_id: str, account_id: str) -> dict[str, str]:
    """One agent's named account, the pair every flow names twice."""

    return {"agent_id": agent_id, "account_id": account_id}


def account(agent_id: str, account_id: str, opening_balance: int) -> dict[str, Any]:
    return {"account": account_ref(agent_id, account_id), "opening_balance": opening_balance}


def rollout_series(series_id: str, *, paths: list[list[int]]) -> dict[str, Any]:
    """One exogenous series, written as one path per rollout.

    The wire form is a single flat row-major block, which a reader cannot check by eye and
    a writer can transpose without producing anything invalid. Per-rollout paths make the
    shape self-evident and the rollout count and snapshot count derived rather than
    restated.
    """

    snapshots = one(
        {len(path) for path in paths},
        too_short=ValueError("a series needs at least one rollout path"),
        too_long=ValueError("every rollout path must cover the same months"),
    )
    return {"series_id": series_id, "snapshots": snapshots, "values": [value for path in paths for value in path]}


def series(
    series_id: str, *, rollout_count: int, snapshots: int, value_at: Callable[[int, int], int]
) -> dict[str, Any]:
    """One exogenous series, materialized from a function of `(rollout, month)`."""

    return rollout_series(
        series_id, paths=[[value_at(rollout, month) for month in range(snapshots)] for rollout in range(rollout_count)]
    )


def shared_series(series_id: str, *, rollout_count: int, path: list[int]) -> dict[str, Any]:
    """A series every rollout follows identically — the case where nothing branches."""

    return rollout_series(series_id, paths=[path] * rollout_count)


def fixture(
    scenario: dict[str, Any], series_specs: list[dict[str, Any]], *, rollout_count: int, currency_quantum: str = "0.01"
) -> dict[str, Any]:
    """Wrap a scenario and its series in the envelope every fixture carries."""

    return {
        "schema_version": SCHEMA_VERSION,
        "currency_code": "USD",
        "currency_quantum": currency_quantum,
        "rollout_count": rollout_count,
        "scenario": scenario,
        "series": series_specs,
    }
