"""Transfer compile output. Pairs with `codec/transfers.py`."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import LevelSeriesKey
from finance.augur.sim.compiler.helpers import (
    AMOUNT_FIXED,
    NO_CODE,
    ORDINARY_DEDUCTION_CATEGORY,
    ORDINARY_INCOME_SOURCE,
    StringTable,
    amount_arrays_cents,
    empty_month_matrix,
    income_source_id,
    slot,
)
from finance.augur.sim.compiler.tax import TaxCompileOutput
from finance.augur.sim.scenario import RecurringTransfer, Scenario, ScheduledTransfer

type TransferLike = ScheduledTransfer | RecurringTransfer


@dataclass(frozen=True)
class TransferCompileOutput:
    """Per-(month, slot) tables for scheduled + recurring transfers. `cause/from_*/to_*`
    identify the parties; `income_profile`/`deduction_profile` route taxable income or
    Schedule-E deductions; `amount_*` is the union-typed amount schedule (fixed or
    series-indexed)."""

    cause: NDArray[np.int64]
    from_agent: NDArray[np.int64]
    from_account: NDArray[np.int64]
    from_slot: NDArray[np.int64]
    to_agent: NDArray[np.int64]
    to_account: NDArray[np.int64]
    to_slot: NDArray[np.int64]
    income_profile: NDArray[np.int64]
    deduction_profile: NDArray[np.int64]
    amount_kind: NDArray[np.int64]
    amount_fixed: NDArray[np.int64]
    amount_base: NDArray[np.int64]
    amount_series: NDArray[np.int64]
    amount_base_month: NDArray[np.int64]
    amount_period: NDArray[np.int64]


def compile_transfer_slots(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    profile_index_by_agent: dict[str, int],
    series_index_by_id: dict[LevelSeriesKey, int],
    tax: TaxCompileOutput,
) -> TransferCompileOutput:
    by_month: list[list[TransferLike]] = []
    max_slots = 0
    horizon = int(scenario.horizon_months)
    for month in range(horizon):
        active: list[TransferLike] = [t for t in scenario.scheduled_transfers if t.month == month]
        active.extend(t for t in scenario.recurring_transfers if t.is_active_at(month))
        by_month.append(active)
        max_slots = max(max_slots, len(active))

    cause = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    income_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    deduction_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_kind = empty_month_matrix(horizon, max_slots, np.int64, AMOUNT_FIXED)
    amount_fixed = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_base = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_series = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_base_month = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_period = empty_month_matrix(horizon, max_slots, np.int64, 1)

    for month, active in enumerate(by_month):
        for idx, transfer in enumerate(active):
            cause[month, idx] = strings.require(transfer.cause_id)
            from_agent[month, idx] = strings.require(transfer.from_agent_id)
            from_account[month, idx] = strings.require(transfer.from_account_id)
            from_slot[month, idx] = slot(account_slot_by_key, transfer.from_agent_id, transfer.from_account_id)
            to_agent[month, idx] = strings.require(transfer.to_agent_id)
            to_account[month, idx] = strings.require(transfer.to_account_id)
            to_slot[month, idx] = slot(account_slot_by_key, transfer.to_agent_id, transfer.to_account_id)
            if transfer.income_category is not None:
                # The row is a (profile, source) BUCKET, so a coupon lands in a different row
                # than wages for the same agent and each jurisdiction can include one and not
                # the other. Deductions still target the ordinary bucket.
                profile_index = profile_index_by_agent.get(transfer.to_agent_id, NO_CODE)
                income_profile[month, idx] = (
                    NO_CODE
                    if profile_index == NO_CODE
                    else tax.income_bucket(profile_index, income_source_id(transfer.income_category))
                )
            if transfer.deduction_category == ORDINARY_DEDUCTION_CATEGORY:
                deduction_index = profile_index_by_agent.get(transfer.from_agent_id, NO_CODE)
                deduction_profile[month, idx] = (
                    NO_CODE
                    if deduction_index == NO_CODE
                    else tax.income_bucket(deduction_index, ORDINARY_INCOME_SOURCE)
                )
            kind, fixed, base, series, base_month, period = amount_arrays_cents(transfer.amount_usd, series_index_by_id)
            amount_kind[month, idx] = kind
            amount_fixed[month, idx] = fixed
            amount_base[month, idx] = base
            amount_series[month, idx] = series
            amount_base_month[month, idx] = base_month
            amount_period[month, idx] = period
    return TransferCompileOutput(
        cause=cause,
        from_agent=from_agent,
        from_account=from_account,
        from_slot=from_slot,
        to_agent=to_agent,
        to_account=to_account,
        to_slot=to_slot,
        income_profile=income_profile,
        deduction_profile=deduction_profile,
        amount_kind=amount_kind,
        amount_fixed=amount_fixed,
        amount_base=amount_base,
        amount_series=amount_series,
        amount_base_month=amount_base_month,
        amount_period=amount_period,
    )
