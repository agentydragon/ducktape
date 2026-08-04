"""Property-linked cashflow compile output.

These rows are property-domain inputs: rent, management fees, and leasing fees
that should stop when the tied property is no longer active. Decode still emits
them into the generic transfer event frame; the property key is only an internal
execution gate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import LevelSeriesKey
from finance.augur.sim.compiler.helpers import (
    AMOUNT_FIXED,
    NO_CODE,
    ORDINARY_DEDUCTION_CATEGORY,
    StringTable,
    amount_arrays_cents,
    empty_month_matrix,
    slot,
)
from finance.augur.sim.compiler.income_buckets import IncomeBuckets
from finance.augur.sim.scenario import RecurringPropertyCashflow, Scenario, ScheduledPropertyCashflow

type PropertyCashflowLike = ScheduledPropertyCashflow | RecurringPropertyCashflow


@dataclass(frozen=True)
class PropertyCashflowCompileOutput:
    """Per-(month, slot) tables for property-linked cashflows."""

    cause: NDArray[np.int64]
    from_agent: NDArray[np.int64]
    from_account: NDArray[np.int64]
    from_slot: NDArray[np.int64]
    to_agent: NDArray[np.int64]
    to_account: NDArray[np.int64]
    to_slot: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    income_profile: NDArray[np.int64]
    deduction_profile: NDArray[np.int64]
    amount_kind: NDArray[np.int64]
    amount_fixed: NDArray[np.int64]
    amount_base: NDArray[np.int64]
    amount_series: NDArray[np.int64]
    amount_base_month: NDArray[np.int64]
    amount_period: NDArray[np.int64]


def compile_property_cashflows(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    profile_index_by_agent: dict[str, int],
    series_index_by_id: dict[LevelSeriesKey, int],
    property_slot_by_id: dict[str, int],
    buckets: IncomeBuckets,
) -> PropertyCashflowCompileOutput:
    by_month: list[list[PropertyCashflowLike]] = []
    max_slots = 0
    horizon = int(scenario.horizon_months)
    for month in range(horizon):
        active: list[PropertyCashflowLike] = [
            cashflow for cashflow in scenario.scheduled_property_cashflows if cashflow.month == month
        ]
        active.extend(cashflow for cashflow in scenario.recurring_property_cashflows if cashflow.is_active_at(month))
        by_month.append(active)
        max_slots = max(max_slots, len(active))

    cause = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    property_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    income_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    deduction_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_kind = empty_month_matrix(horizon, max_slots, np.int64, AMOUNT_FIXED)
    amount_fixed = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_base = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_series = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_base_month = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_period = empty_month_matrix(horizon, max_slots, np.int64, 1)

    for month, active in enumerate(by_month):
        for idx, cashflow in enumerate(active):
            cause[month, idx] = strings.require(cashflow.cause_id)
            from_agent[month, idx] = strings.require(cashflow.from_agent_id)
            from_account[month, idx] = strings.require(cashflow.from_account_id)
            from_slot[month, idx] = slot(account_slot_by_key, cashflow.from_agent_id, cashflow.from_account_id)
            to_agent[month, idx] = strings.require(cashflow.to_agent_id)
            to_account[month, idx] = strings.require(cashflow.to_account_id)
            to_slot[month, idx] = slot(account_slot_by_key, cashflow.to_agent_id, cashflow.to_account_id)
            try:
                property_slot[month, idx] = property_slot_by_id[cashflow.property_id]
            except KeyError as exc:
                known = ", ".join(repr(property_id) for property_id in sorted(property_slot_by_id))
                raise ValueError(
                    f"property cashflow {cashflow.cause_id!r} references unknown property_id "
                    f"{cashflow.property_id!r}; known: {known or '<none>'}"
                ) from exc
            if cashflow.income_category is not None:
                income_profile[month, idx] = buckets.bucket(
                    profile_index_by_agent.get(cashflow.to_agent_id, NO_CODE), cashflow.income_category
                )
            if cashflow.deduction_category == ORDINARY_DEDUCTION_CATEGORY:
                deduction_profile[month, idx] = buckets.ordinary_bucket(
                    profile_index_by_agent.get(cashflow.from_agent_id, NO_CODE)
                )
            kind, fixed, base, series, base_month, period = amount_arrays_cents(cashflow.amount_usd, series_index_by_id)
            amount_kind[month, idx] = kind
            amount_fixed[month, idx] = fixed
            amount_base[month, idx] = base
            amount_series[month, idx] = series
            amount_base_month[month, idx] = base_month
            amount_period[month, idx] = period

    return PropertyCashflowCompileOutput(
        cause=cause,
        from_agent=from_agent,
        from_account=from_account,
        from_slot=from_slot,
        to_agent=to_agent,
        to_account=to_account,
        to_slot=to_slot,
        property_slot=property_slot,
        income_profile=income_profile,
        deduction_profile=deduction_profile,
        amount_kind=amount_kind,
        amount_fixed=amount_fixed,
        amount_base=amount_base,
        amount_series=amount_series,
        amount_base_month=amount_base_month,
        amount_period=amount_period,
    )
