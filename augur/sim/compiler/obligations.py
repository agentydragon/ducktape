"""Obligation compile output. Pairs with `codec/obligations.py`.

Obligations cover a heterogeneous union of scenario-level obligations + mortgage
payments + property-tax accruals + estimated-tax quarterly payments + tax true-ups.
The `source_kind`/`source_index` discriminator drives the engine's dispatch and is
re-purposed across kinds — see the per-kind dispatch in `engine.phases` and the
B5 follow-up that tracks bundling this into typed views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from augur.model.series import LevelSeriesKey
from augur.sim.compiler.helpers import (
    AMOUNT_FIXED,
    NO_CODE,
    ORDINARY_DEDUCTION_CATEGORY,
    StringTable,
    amount_arrays,
    empty_month_matrix,
    slot,
)
from augur.sim.compiler.properties import LiabilityCompileOutput, PropertyCompileOutput
from augur.sim.compiler.tax import TaxCompileOutput
from augur.sim.scenario import RecurringObligation, Scenario, ScheduledObligation


@dataclass(frozen=True)
class ObligationCompileOutput:
    """Per-(month, slot) obligation plumbing covering scheduled/recurring
    obligations + mortgage payments + property-tax accruals + estimated-tax/
    true-up payments. `source_kind`/`source_index` discriminate which subsystem
    drives this slot (kind 0 = scenario obligation, 1 = mortgage, 2 = property
    tax, 3 = quarterly estimated tax, 4 = Q4 estimate, 5 = year-end true-up).
    `property_tax_profile` + `property_slot` are populated for kind==2 only
    (NO_CODE elsewhere). `deduction_profile`/`deductible_fraction` route
    Schedule-E deductions for obligations whose `deduction_category` was set."""

    cause: NDArray[np.int64]
    id: NDArray[np.int64]
    type: NDArray[np.int64]
    agent: NDArray[np.int64]
    from_account: NDArray[np.int64]
    from_slot: NDArray[np.int64]
    to_agent: NDArray[np.int64]
    to_account: NDArray[np.int64]
    to_slot: NDArray[np.int64]
    amount_kind: NDArray[np.int64]
    amount_fixed: NDArray[np.float64]
    amount_base: NDArray[np.float64]
    amount_series: NDArray[np.int64]
    amount_base_month: NDArray[np.int64]
    amount_period: NDArray[np.int64]
    source_kind: NDArray[np.int64]
    source_index: NDArray[np.int64]
    property_tax_profile: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    deduction_profile: NDArray[np.int64]
    deductible_fraction: NDArray[np.float64]


def estimated_tax_quarter(month: int) -> int | None:
    month_in_year = month % 12
    if month_in_year == 3:
        return 1
    if month_in_year == 5:
        return 2
    if month_in_year == 8:
        return 3
    if month_in_year == 0 and month > 0:
        return 4
    return None


def compile_obligation_slots(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[LevelSeriesKey, int],
    properties: PropertyCompileOutput,
    property_slot_by_id: dict[str, int],
    liabilities: LiabilityCompileOutput,
    tax: TaxCompileOutput,
) -> ObligationCompileOutput:
    horizon = int(scenario.horizon_months)
    monthly_specs: list[list[dict[str, Any]]] = [[] for _ in range(horizon)]

    for scheduled in scenario.scheduled_obligations:
        if 0 <= scheduled.month < horizon:
            monthly_specs[scheduled.month].append({"kind": 0, "source": NO_CODE, "config": scheduled})
    for month in range(horizon):
        for recurring in scenario.recurring_obligations:
            if recurring.is_active_at(month):
                monthly_specs[month].append({"kind": 0, "source": NO_CODE, "config": recurring})

    for month in range(horizon):
        for liability_slot, liability_code in enumerate(liabilities.codes.tolist()):
            prop_slot = int(liabilities.property_slot[liability_slot])
            monthly_specs[month].append(
                {"kind": 1, "source": liability_slot, "liability_code": liability_code, "prop_slot": prop_slot}
            )

    for month in range(horizon):
        for prop_slot, prop_code in enumerate(properties.id.tolist()):
            if prop_slot < properties.month.shape[0]:
                monthly_specs[month].append({"kind": 2, "source": prop_slot, "property_code": prop_code})

    for month in range(horizon):
        quarter = estimated_tax_quarter(month)
        if quarter in {1, 2, 3}:
            for profile_index, prior_year_tax in enumerate(tax.profile_prior_year_tax.tolist()):
                if prior_year_tax > 0:
                    monthly_specs[month].append({"kind": 3, "source": profile_index, "quarter": quarter})
        elif quarter == 4:
            tax_year = month // 12 - 1
            if tax_year >= 0:
                for profile_index in range(len(tax.profile_prior_year_tax)):
                    monthly_specs[month].append({"kind": 4, "source": profile_index, "tax_year": tax_year})
                    monthly_specs[month].append({"kind": 5, "source": profile_index, "tax_year": tax_year})

    max_slots = max(1, max((len(specs) for specs in monthly_specs), default=0))
    cause = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    obligation_id = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    obligation_type = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_agent = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_account = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_slot = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_kind = empty_month_matrix(horizon, max_slots, np.int64, AMOUNT_FIXED)
    amount_fixed = empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_base = empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_series = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_base_month = empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_period = empty_month_matrix(horizon, max_slots, np.int64, 1)
    source_kind = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    source_index = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    # Default NO_CODE; populated only for property-tax obligations whose owner has a TaxProfile.
    property_tax_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    # Property slot for property-tax obligations. NO_CODE elsewhere.
    property_slot_matrix = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    # Schedule E deduction wiring: NO_CODE / 0.0 unless the obligation declares
    # deduction_category. Engine decrements ordinary_ytd by amount × deductible_fraction
    # at settlement time when deduction_profile >= 0.
    deduction_profile = empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    deductible_fraction = empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    agent_to_profile_index: dict[int, int] = {
        strings.require(p.agent_id): i for i, p in enumerate(scenario.tax_profiles)
    }

    profile_by_index = scenario.tax_profiles
    for month, specs in enumerate(monthly_specs):
        for idx, spec in enumerate(specs):
            source_kind[month, idx] = int(spec["kind"])
            source_index[month, idx] = int(spec["source"])
            if spec["kind"] == 0:
                config = spec["config"]
                assert isinstance(config, ScheduledObligation | RecurringObligation)
                cause_text = f"{config.obligation_id}_m{month}"
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require(config.obligation_type)
                agent[month, idx] = strings.require(config.agent_id)
                from_account[month, idx] = strings.require(config.from_account_id)
                from_slot[month, idx] = slot(account_slot_by_key, config.agent_id, config.from_account_id)
                to_agent[month, idx] = strings.require(config.to_agent_id)
                to_account[month, idx] = strings.require(config.to_account_id)
                to_slot[month, idx] = slot(account_slot_by_key, config.to_agent_id, config.to_account_id)
                kind, fixed, base, series, base_month, period = amount_arrays(config.amount_due_usd, series_index_by_id)
                amount_kind[month, idx] = kind
                amount_fixed[month, idx] = fixed
                amount_base[month, idx] = base
                amount_series[month, idx] = series
                amount_base_month[month, idx] = base_month
                amount_period[month, idx] = period
                if config.deduction_category == ORDINARY_DEDUCTION_CATEGORY:
                    profile = agent_to_profile_index.get(strings.require(config.agent_id), NO_CODE)
                    deduction_profile[month, idx] = profile
                    deductible_fraction[month, idx] = float(config.deductible_fraction)
                # Tie the obligation to a property if requested so the engine reads runtime
                # rented_fraction at settlement time instead of the compile-time fraction.
                if config.property_id is not None:
                    if config.property_id not in property_slot_by_id:
                        raise ValueError(
                            f"Obligation {config.obligation_id!r} references unknown property_id {config.property_id!r}"
                        )
                    property_slot_matrix[month, idx] = property_slot_by_id[config.property_id]
            elif spec["kind"] in {1, 2, 3, 4, 5}:
                # The dynamic source fields are decoded later from source_kind/source_index.
                continue

    # Fill dynamic source metadata after all strings that profiles/properties need are interned.
    for month, specs in enumerate(monthly_specs):
        for idx, spec in enumerate(specs):
            kind = int(spec["kind"])
            if kind == 1:
                liability_slot = int(spec["source"])
                if liability_slot >= liabilities.property_slot.shape[0]:
                    continue
                purchase = scenario.scheduled_property_purchases[int(liabilities.property_slot[liability_slot])]
                if purchase.mortgage is None:
                    continue
                cause_text = f"{purchase.mortgage.liability_id}_payment_m{month}"
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require("mortgage_payment")
                agent[month, idx] = strings.require(purchase.buyer_agent_id)
                from_account[month, idx] = strings.require(purchase.buyer_account_id)
                from_slot[month, idx] = slot(account_slot_by_key, purchase.buyer_agent_id, purchase.buyer_account_id)
                to_agent[month, idx] = strings.require(purchase.mortgage.lender_agent_id)
                to_account[month, idx] = strings.require(purchase.mortgage.lender_account_id)
                to_slot[month, idx] = slot(
                    account_slot_by_key, purchase.mortgage.lender_agent_id, purchase.mortgage.lender_account_id
                )
            elif kind == 2:
                prop_slot = int(spec["source"])
                if prop_slot >= len(scenario.scheduled_property_purchases):
                    continue
                purchase = scenario.scheduled_property_purchases[prop_slot]
                policy = next(
                    (
                        p
                        for p in scenario.property_tax_policies
                        if p.property_id == purchase.property_id and p.is_active_at(month)
                    ),
                    None,
                )
                if policy is None:
                    source_kind[month, idx] = NO_CODE
                    continue
                cause_text = f"{policy.property_id}_property_tax_m{month}"
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require("property_tax")
                agent[month, idx] = strings.require(policy.owner_agent_id)
                from_account[month, idx] = strings.require(policy.from_account_id)
                from_slot[month, idx] = slot(account_slot_by_key, policy.owner_agent_id, policy.from_account_id)
                to_agent[month, idx] = strings.require(policy.tax_authority_agent_id)
                to_account[month, idx] = strings.require(policy.tax_authority_account_id)
                to_slot[month, idx] = slot(
                    account_slot_by_key, policy.tax_authority_agent_id, policy.tax_authority_account_id
                )
                amount_fixed[month, idx] = (
                    float(policy.annual_tax_rate) if policy.annual_tax_rate is not None else np.nan
                )
                owner_code = strings.require(policy.owner_agent_id)
                owner_profile = agent_to_profile_index.get(owner_code, NO_CODE)
                property_tax_profile[month, idx] = owner_profile
                # Wire the property slot so the engine can look up runtime rented_fraction at
                # settlement time. SALT/Schedule E split moves with mid-horizon lifecycle events.
                property_slot_matrix[month, idx] = prop_slot
                rented_fraction_val = float(purchase.rented_fraction)
                if owner_profile >= 0:
                    deduction_profile[month, idx] = owner_profile
                    deductible_fraction[month, idx] = rented_fraction_val
            elif kind in {3, 4, 5}:
                profile_index = int(spec["source"])
                tax_profile = profile_by_index[profile_index]
                if kind == 3:
                    quarter = int(spec["quarter"])
                    tax_year = month // 12
                    cause_text = f"{tax_profile.agent_id}_estimated_tax_q{quarter}_y{tax_year}"
                    obligation_type_text = "estimated_tax"
                elif kind == 4:
                    tax_year = int(spec["tax_year"])
                    cause_text = f"{tax_profile.agent_id}_estimated_tax_q4_y{tax_year}"
                    obligation_type_text = "estimated_tax"
                else:
                    tax_year = int(spec["tax_year"])
                    cause_text = f"{tax_profile.agent_id}_tax_true_up_y{tax_year}"
                    obligation_type_text = "tax_true_up"
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require(obligation_type_text)
                agent[month, idx] = strings.require(tax_profile.agent_id)
                from_account[month, idx] = strings.require(tax_profile.payment_account_id)
                from_slot[month, idx] = slot(account_slot_by_key, tax_profile.agent_id, tax_profile.payment_account_id)
                to_agent[month, idx] = strings.require(tax_profile.tax_authority_agent_id)
                to_account[month, idx] = strings.require(tax_profile.tax_authority_account_id)
                to_slot[month, idx] = slot(
                    account_slot_by_key, tax_profile.tax_authority_agent_id, tax_profile.tax_authority_account_id
                )
    return ObligationCompileOutput(
        cause=cause,
        id=obligation_id,
        type=obligation_type,
        agent=agent,
        from_account=from_account,
        from_slot=from_slot,
        to_agent=to_agent,
        to_account=to_account,
        to_slot=to_slot,
        amount_kind=amount_kind,
        amount_fixed=amount_fixed,
        amount_base=amount_base,
        amount_series=amount_series,
        amount_base_month=amount_base_month,
        amount_period=amount_period,
        source_kind=source_kind,
        source_index=source_index,
        property_tax_profile=property_tax_profile,
        property_slot=property_slot_matrix,
        deduction_profile=deduction_profile,
        deductible_fraction=deductible_fraction,
    )
