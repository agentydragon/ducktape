"""Property lifecycle event compile output. Pairs with `codec/lifecycle.py`.

A `PropertyLifecycleEvent` row (SetRentedFractionEvent, CapitalImprovementEvent,
or PropertySaleEvent in the scenario layer) is lowered into a single dense
SoA table here; the engine's `_apply_lifecycle_events` phase scans the relevant
month range via `month_starts` and dispatches on `kind`."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.enums import LifecycleKind
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.scenario import CapitalImprovementEvent, PropertySaleEvent, Scenario, SetRentedFractionEvent


@dataclass(frozen=True)
class LifecycleEventCompileOutput:
    """PropertyLifecycleEvent rows compiled into per-month sparse storage. Sorted by
    month so the engine scans a per-month index range via `month_starts`:
    `events_for_month_M = events[month_starts[M]:month_starts[M+1]]`. `kind[i]` is
    `LifecycleKind.FRACTION` (0) for rented-fraction change (start/stop/change-rental
    -plan), `LifecycleKind.CAPITAL_IMPROVEMENT` (1) for cash + basis bump, or
    `LifecycleKind.SALE` (2). `rented_fraction[i]` is the new value (kind 0; 0.0
    otherwise). `amount[i]` is the USD spend (kind 1), the closing-cost percentage
    (kind 2; 0..100), or 0.0 (kind 0). `month_starts` has length `horizon_months + 1`
    so the engine can do `events[starts[M]:starts[M+1]]` for any month M."""

    month: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    kind: NDArray[np.int64]
    rented_fraction: NDArray[np.float64]
    amount: NDArray[np.float64]
    amount_cents: NDArray[np.int64]
    month_starts: NDArray[np.int64]


def compile_lifecycle_events(scenario: Scenario, property_slot_by_id: dict[str, int]) -> LifecycleEventCompileOutput:
    events_sorted = sorted(scenario.property_lifecycle_events, key=lambda e: (int(e.month), e.property_id))
    count = len(events_sorted)
    month = np.empty(count, dtype=np.int64)
    property_slot = np.empty(count, dtype=np.int64)
    kind = np.empty(count, dtype=np.int64)
    rented_fraction = np.zeros(count, dtype=np.float64)
    amount = np.zeros(count, dtype=np.float64)
    amount_cents = np.zeros(count, dtype=np.int64)
    for i, event in enumerate(events_sorted):
        if event.property_id not in property_slot_by_id:
            raise ValueError(
                f"PropertyLifecycleEvent at month {event.month} references unknown property_id "
                f"{event.property_id!r}; known: {sorted(property_slot_by_id)}"
            )
        slot = property_slot_by_id[event.property_id]
        purchase_month = int(scenario.scheduled_property_purchases[slot].month)
        if int(event.month) <= purchase_month:
            raise ValueError(
                f"PropertyLifecycleEvent for {event.property_id!r} fires at month {event.month} "
                f"but the property's purchase month is {purchase_month}; lifecycle events must "
                "fire strictly after purchase."
            )
        month[i] = int(event.month)
        property_slot[i] = slot
        if isinstance(event, SetRentedFractionEvent):
            kind[i] = LifecycleKind.FRACTION
            rented_fraction[i] = float(event.rented_fraction)
        elif isinstance(event, CapitalImprovementEvent):
            kind[i] = LifecycleKind.CAPITAL_IMPROVEMENT
            amount[i] = float(event.amount_usd)
            amount_cents[i] = usd_to_cents(event.amount_usd)
        elif isinstance(event, PropertySaleEvent):
            kind[i] = LifecycleKind.SALE
            # Reuse `amount` as closing_cost_pct for sale events (different semantic per kind,
            # but storing in the same dense column avoids another array).
            amount[i] = float(event.closing_cost_pct)
        else:
            raise TypeError(f"unknown PropertyLifecycleEvent variant: {type(event).__name__}")
    # `starts[M]` = first event index for month >= M; `starts[H]` = count.
    month_starts = np.searchsorted(month, np.arange(int(scenario.horizon_months) + 1), side="left").astype(np.int64)
    return LifecycleEventCompileOutput(
        month=month,
        property_slot=property_slot,
        kind=kind,
        rented_fraction=rented_fraction,
        amount=amount,
        amount_cents=amount_cents,
        month_starts=month_starts,
    )
