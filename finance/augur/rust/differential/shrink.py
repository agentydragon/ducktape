"""Reduce a diverging case to one a person can read.

A fuzzer's finding is only useful if someone can look at it. A sixty-month, sixteen-agent
scenario that disagrees somewhere is nearly as opaque as no scenario at all, so a divergence
is shrunk before it is reported: drop scenario entries, shorten the horizon, drop rollouts,
and flatten series, keeping every reduction that still reproduces the *same* divergence.

Same, not any: the predicate a caller supplies compares the failing channel, because a
reduction that swaps one disagreement for another has moved the finding rather than shrunk
it. Every candidate costs a run on both engines and usually a fresh XLA compile, so the
search is greedy and capped rather than exhaustive.

A candidate is assembled rather than validated: a reduction that leaves a dangling reference,
or a scenario the compiler or the Rust validator refuses, simply fails the predicate and is
discarded, so the reductions need no dependency order between them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import polars as pl
from pydantic import BaseModel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import LevelSeriesKey
from finance.augur.sim.scenario import (
    Agent,
    CapitalImprovementEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
)
from finance.augur.sim.testing.case import Case

logger = logging.getLogger(__name__)

# The agent roster is derived from the accounts rather than dropped on its own: an agent with
# no account is what dropping an account leaves behind, and every other reference to it would
# dangle. Everything else the scenario lists is dropped one entry at a time.
_DERIVED_FIELDS = frozenset({"agents", "initial_cash"})


def _rescheduled(entry: BaseModel, horizon_months: int) -> BaseModel | None:
    """One entry as it stands in a shorter horizon, or `None` where it no longer fits.

    Dropping is not optional. An entry scheduled past the horizon is a scenario neither engine
    accepts, so leaving one behind produces a candidate that cannot answer the question.
    """

    match entry:
        case (
            ScheduledTransfer()
            | ScheduledPropertyCashflow()
            | ScheduledObligation()
            | ScheduledAssetSale()
            | ScheduledPropertyPurchase()
            | SetPrimaryResidenceEvent()
            | SetRentedFractionEvent()
            | PropertySaleEvent()
            | CapitalImprovementEvent()
        ):
            return entry if entry.month < horizon_months else None
        case RecurringTransfer() | RecurringPropertyCashflow() | RecurringObligation() | PropertyTaxPolicy():
            if entry.start_month >= horizon_months:
                return None
            if entry.end_month is None or entry.end_month < horizon_months:
                return entry
            return entry.model_copy(update={"end_month": horizon_months - 1})
    return entry


def _list_fields(scenario: Scenario) -> dict[str, list[Any]]:
    """Every scenario field holding entries, read off the model rather than listed here.

    A shrinker carrying its own roster silently stops reducing whatever the scenario grows
    next, so the fields are discovered; `getattr` is how a generic reducer reaches a field it
    is not written to know about.
    """

    values = {name: getattr(scenario, name) for name in type(scenario).model_fields}
    return {name: value for name, value in values.items() if isinstance(value, list) and name not in _DERIVED_FIELDS}


def _rebuilt(scenario: Scenario, **update: Any) -> Scenario:
    return scenario.model_copy(update=update)


def _trimmed_private_equity(
    private_equity: PrivateEquityBundle, *, rollout_count: int, horizon_months: int
) -> PrivateEquityBundle:
    if private_equity.is_empty():
        return private_equity
    return PrivateEquityBundle(
        frame=private_equity.frame.filter(
            (pl.col("rollout_index") < rollout_count) & (pl.col("month_index") <= horizon_months)
        )
    )


def _with_horizon(case: Case, horizon_months: int) -> Case:
    """The same case over fewer months: series trimmed, entries past the end dropped."""

    scenario = _rebuilt(
        case.scenario,
        horizon_months=horizon_months,
        **{
            name: [kept for entry in entries if (kept := _rescheduled(entry, horizon_months)) is not None]
            for name, entries in _list_fields(case.scenario).items()
        },
    )
    return Case(
        scenario=scenario,
        rollout_count=case.rollout_count,
        series={key: path[:, : horizon_months + 1] for key, path in case.series.items()},
        private_equity=_trimmed_private_equity(
            case.private_equity, rollout_count=case.rollout_count, horizon_months=horizon_months
        ),
        locations=case.locations,
    )


def _with_rollouts(case: Case, rollout_count: int) -> Case:
    return Case(
        scenario=case.scenario,
        rollout_count=rollout_count,
        series={key: path[:rollout_count] for key, path in case.series.items()},
        private_equity=_trimmed_private_equity(
            case.private_equity, rollout_count=rollout_count, horizon_months=int(case.scenario.horizon_months)
        ),
        locations=case.locations,
    )


def _without_entry(case: Case, name: str, index: int) -> Case:
    entries = [entry for position, entry in enumerate(_list_fields(case.scenario)[name]) if position != index]
    return _with_scenario(case, _rebuilt(case.scenario, **{name: entries}))


def _without_account(case: Case, index: int) -> Case:
    """One opening balance dropped, and the agent roster re-derived from what is left."""

    balances = [balance for position, balance in enumerate(case.scenario.initial_cash) if position != index]
    agents = [Agent(agent_id=agent_id) for agent_id in sorted({balance.agent_id for balance in balances})]
    return _with_scenario(case, _rebuilt(case.scenario, initial_cash=balances, agents=agents))


def _with_scenario(case: Case, scenario: Scenario) -> Case:
    return Case(
        scenario=scenario,
        rollout_count=case.rollout_count,
        series=case.series,
        private_equity=case.private_equity,
        locations=case.locations,
    )


def _flat_series(case: Case, key: LevelSeriesKey) -> Case:
    """One series held at its month-0 level throughout, which kills a price path at once."""

    path = case.series[key]
    return Case(
        scenario=case.scenario,
        rollout_count=case.rollout_count,
        series={**case.series, key: np.repeat(path[:, :1], path.shape[1], axis=1)},
        private_equity=case.private_equity,
        locations=case.locations,
    )


def _same(left: Case, right: Case) -> bool:
    """Whether two cases state the same thing, so a reduction that changed nothing is dropped.

    A candidate that changes nothing — flattening an already-flat series, or asking for the
    rollout count it already has — would be accepted forever by a predicate that keeps saying
    yes, so the search would never terminate.
    """

    return (
        left.scenario == right.scenario
        and left.rollout_count == right.rollout_count
        and left.locations == right.locations
        and left.series.keys() == right.series.keys()
        and all(np.array_equal(path, right.series[key]) for key, path in left.series.items())
        and left.private_equity.frame.equals(right.private_equity.frame)
    )


def _candidates(case: Case) -> list[Case]:
    """Every one-step reduction of `case`, biggest bites first."""

    horizon = int(case.scenario.horizon_months)
    reductions = [_with_horizon(case, months) for months in (horizon // 2, horizon - 1) if months >= 1]
    reductions += [_with_rollouts(case, count) for count in (1, case.rollout_count - 1) if count >= 1]
    reductions += [
        _without_entry(case, name, index)
        for name, entries in _list_fields(case.scenario).items()
        for index in reversed(range(len(entries)))
    ]
    reductions += [_without_account(case, index) for index in reversed(range(len(case.scenario.initial_cash)))]
    reductions += [_flat_series(case, key) for key in case.series]
    return [candidate for candidate in reductions if not _same(candidate, case)]


def shrink_case(
    case: Case,
    *,
    still_diverges: Callable[[Case], bool],
    max_candidates: int = 400,
    budget_seconds: float = float("inf"),
) -> tuple[Case, int]:
    """Greedily reduce `case` while `still_diverges`, returning it and the candidates tried.

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
        for candidate in _candidates(case):
            if tried >= max_candidates or time.monotonic() >= deadline:
                break
            tried += 1
            if _reproduces(candidate, still_diverges):
                case, reduced = candidate, True
                break
    return case, tried


def _reproduces(candidate: Case, still_diverges: Callable[[Case], bool]) -> bool:
    """`still_diverges`, with a candidate that blows up counted as not reproducing.

    A reduction can leave a case one of the engines will not take, and losing the whole
    finding to that would be the wrong trade — so it is logged and skipped rather than
    raised. A run whose log fills with these has a shrinker bug, not a shrinker limitation.
    """

    try:
        return still_diverges(candidate)
    except Exception:
        logger.warning("shrink candidate rejected", exc_info=True)
        return False
