"""Reduce a diverging fixture to one a person can read.

A fuzzer's finding is only useful if someone can look at it. A sixty-month, sixteen-agent
fixture that disagrees somewhere is nearly as opaque as no fixture at all, so a divergence is
shrunk before it is reported: drop scenario entries, shorten the horizon, drop rollouts, and
flatten series, keeping every reduction that still reproduces the *same* divergence.

Same, not any: the predicate a caller supplies compares the failing channel, because a
reduction that swaps one disagreement for another has moved the finding rather than shrunk
it. Every candidate costs a run on both engines and usually a fresh XLA compile, so the
search is greedy and capped rather than exhaustive.
"""

import copy
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Scenario lists whose entries a candidate may drop one at a time. A reduction that leaves a
# dangling reference just fails the predicate and is discarded, so the list needs no
# dependency order.
DROPPABLE_KEYS = (
    "scheduled_transfers",
    "recurring_transfers",
    "scheduled_property_cashflows",
    "recurring_property_cashflows",
    "obligations",
    "recurring_obligations",
    "scheduled_sales",
    "initial_lots",
    "initial_bonds",
    "distributions",
    "target_allocation_policies",
    "private_equity_tender_policies",
    "harvest_policies",
    "tax_profiles",
    "property_rented_fraction_events",
    "capital_improvement_events",
    "property_sales",
    "primary_residence_events",
    "initial_primary_residences",
    "mortgage_interest_deduction_policies",
    "property_tax_policies",
    "federal_salt_deduction_policies",
    "scheduled_property_purchases",
    "accounts",
)


def _in_horizon(entry: Any, horizon_months: int) -> bool:
    return not (isinstance(entry, dict) and max(entry.get("month", 0), entry.get("start_month", 0)) >= horizon_months)


def _with_horizon(fixture: dict[str, Any], horizon_months: int) -> dict[str, Any]:
    """The same fixture over fewer months: series trimmed, entries past the end dropped.

    Dropping is not optional. An entry scheduled past the horizon is not a fixture either
    engine accepts — the Rust validator refuses it and the JAX adapter indexes off the end of
    the series — so leaving one behind produces a candidate that cannot answer the question.
    """

    candidate = copy.deepcopy(fixture)
    scenario = candidate["scenario"]
    old_snapshots = scenario["horizon_months"] + 1
    snapshots = horizon_months + 1
    scenario["horizon_months"] = horizon_months
    for key, value in scenario.items():
        if isinstance(value, list):
            scenario[key] = [entry for entry in value if _in_horizon(entry, horizon_months)]
    for entry in (entry for value in scenario.values() if isinstance(value, list) for entry in value):
        if isinstance(entry, dict) and entry.get("end_month") is not None:
            entry["end_month"] = min(entry["end_month"], horizon_months - 1)
    for series in candidate["series"]:
        rollouts = len(series["values"]) // old_snapshots
        series["values"] = [
            series["values"][rollout * old_snapshots + month]
            for rollout in range(rollouts)
            for month in range(snapshots)
        ]
        series["snapshots"] = snapshots
    return candidate


def _with_rollouts(fixture: dict[str, Any], rollout_count: int) -> dict[str, Any]:
    candidate = copy.deepcopy(fixture)
    candidate["rollout_count"] = rollout_count
    for series in candidate["series"]:
        series["values"] = series["values"][: rollout_count * series["snapshots"]]
    return candidate


def _without_entry(fixture: dict[str, Any], key: str, index: int) -> dict[str, Any]:
    candidate = copy.deepcopy(fixture)
    del candidate["scenario"][key][index]
    return candidate


def _flat_series(fixture: dict[str, Any], series_index: int) -> dict[str, Any]:
    """One series held at its first value throughout, which kills a whole price path at once."""

    candidate = copy.deepcopy(fixture)
    series = candidate["series"][series_index]
    series["values"] = [series["values"][0]] * len(series["values"])
    return candidate


def _candidates(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Every one-step reduction of `fixture`, biggest bites first."""

    scenario = fixture["scenario"]
    horizon = scenario["horizon_months"]
    reductions = [_with_horizon(fixture, months) for months in (horizon // 2, horizon - 1) if months >= 1]
    reductions += [_with_rollouts(fixture, count) for count in (1, fixture["rollout_count"] - 1) if count >= 1]
    reductions += [
        _without_entry(fixture, key, index)
        for key in DROPPABLE_KEYS
        for index in reversed(range(len(scenario.get(key, []))))
    ]
    reductions += [_flat_series(fixture, index) for index in range(len(fixture["series"]))]
    # A reduction that changes nothing — flattening an already-flat series, or asking for the
    # rollout count it already has — would be accepted forever by a predicate that keeps
    # saying yes.
    return [candidate for candidate in reductions if candidate != fixture]


def shrink_fixture(
    fixture: dict[str, Any],
    *,
    still_diverges: Callable[[dict[str, Any]], bool],
    max_candidates: int = 400,
    budget_seconds: float = float("inf"),
) -> tuple[dict[str, Any], int]:
    """Greedily reduce `fixture` while `still_diverges`, returning it and the candidates tried.

    The loop restarts from the top after every accepted reduction, so a drop that unlocks
    another one is taken in the same pass.

    Both bounds are needed and neither substitutes for the other. `max_candidates` bounds the
    work for a caller whose predicate is cheap, and keeps the search reproducible.
    `budget_seconds` bounds the wall clock for the real one, where nearly every candidate
    changes the plan structure and so pays a fresh XLA compile: a partly shrunk reproducer
    beats a test killed by its Bazel timeout with nothing to show.
    """

    deadline = time.monotonic() + budget_seconds
    tried = 0
    reduced = True
    while reduced and tried < max_candidates and time.monotonic() < deadline:
        reduced = False
        for candidate in _candidates(fixture):
            if tried >= max_candidates or time.monotonic() >= deadline:
                break
            tried += 1
            if _reproduces(candidate, still_diverges):
                fixture, reduced = candidate, True
                break
    return fixture, tried


def _reproduces(candidate: dict[str, Any], still_diverges: Callable[[dict[str, Any]], bool]) -> bool:
    """`still_diverges`, with a candidate that blows up counted as not reproducing.

    A reduction can leave a fixture one of the engines will not take, and losing the whole
    finding to that would be the wrong trade — so it is logged and skipped rather than
    raised. A run whose log fills with these has a shrinker bug, not a shrinker limitation.
    """

    try:
        return still_diverges(candidate)
    except Exception:
        logger.warning("shrink candidate rejected", exc_info=True)
        return False
