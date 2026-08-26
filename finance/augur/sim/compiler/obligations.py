"""Typed obligation-source compilation.

Each obligation source owns a narrow plan. The plans share one dense payment-slot
axis so the engine can merge their typed payment batches and run one common
funding/settlement operation without a source discriminator union.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from jaxtyping import Bool, Float64, Int64

from finance.augur.model.series import LevelSeriesKey
from finance.augur.sim.compiler.helpers import (
    AMOUNT_FIXED,
    NO_CODE,
    ORDINARY_DEDUCTION_CATEGORY,
    AccountSlots,
    StringTable,
    amount_arrays_quanta,
    empty_month_matrix,
)
from finance.augur.sim.compiler.properties import LiabilityCompileOutput, PropertyCompileOutput
from finance.augur.sim.compiler.tax import TaxCompileOutput, TaxLiabilityCompileOutput
from finance.augur.sim.scenario import RecurringObligation, Scenario, ScheduledObligation


@dataclass(frozen=True)
class ObligationPaymentMetadata:
    """Wire/scatter metadata and shared settlement routing for payment slots."""

    cause: Int64[np.ndarray, " month obligation"]
    id: Int64[np.ndarray, " month obligation"]
    type: Int64[np.ndarray, " month obligation"]
    agent: Int64[np.ndarray, " month obligation"]
    from_account: Int64[np.ndarray, " month obligation"]
    from_slot: Int64[np.ndarray, " month obligation"]
    to_agent: Int64[np.ndarray, " month obligation"]
    to_account: Int64[np.ndarray, " month obligation"]
    to_slot: Int64[np.ndarray, " month obligation"]
    property_tax_profile: Int64[np.ndarray, " month obligation"]
    property_slot: Int64[np.ndarray, " month obligation"]
    deduction_profile: Int64[np.ndarray, " month obligation"]
    deductible_fraction: Float64[np.ndarray, " month obligation"]


class ConfiguredObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    amount_kind: ArrayT
    amount_fixed: ArrayT
    amount_base: ArrayT
    amount_series: ArrayT
    amount_base_month: ArrayT
    amount_period: ArrayT


class PropertyTaxObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    amount: ArrayT
    property_purchase_month: ArrayT


class MortgageObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    liability_slot: ArrayT
    annual_rate: ArrayT
    property_purchase_month: ArrayT


class EstimatedTaxObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    quarterly_amount: ArrayT


class Q4EstimatedTaxObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    prior_year_tax: ArrayT
    tax_liability_selector: ArrayT


class PriorYearTaxObligationExecution[ArrayT](NamedTuple):
    active: ArrayT
    profile_index: ArrayT
    prior_year_tax: ArrayT
    tax_liability_selector: ArrayT
    tax_year_end_month: ArrayT


class ObligationMetadataExecution[ArrayT](NamedTuple):
    agent: ArrayT
    from_slot: ArrayT
    to_slot: ArrayT
    deduction_profile: ArrayT
    deductible_fraction: ArrayT
    property_tax_profile: ArrayT
    property_slot: ArrayT


class ObligationExecution[ArrayT](NamedTuple):
    metadata: ObligationMetadataExecution[ArrayT]
    configured: ConfiguredObligationExecution[ArrayT]
    property_tax: PropertyTaxObligationExecution[ArrayT]
    mortgage: MortgageObligationExecution[ArrayT]
    estimated_tax: EstimatedTaxObligationExecution[ArrayT]
    q4_estimated_tax: Q4EstimatedTaxObligationExecution[ArrayT]
    tax_true_up: PriorYearTaxObligationExecution[ArrayT]


@dataclass(frozen=True)
class ObligationCompileOutput:
    """Decode metadata plus the canonical execution PyTree."""

    metadata: ObligationPaymentMetadata
    execution: ObligationExecution[np.ndarray]


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
    account_slot_by_key: AccountSlots,
    series_index_by_id: dict[LevelSeriesKey, int],
    properties: PropertyCompileOutput,
    property_slot_by_id: dict[str, int],
    liabilities: LiabilityCompileOutput,
    tax: TaxCompileOutput,
    tax_liabilities: TaxLiabilityCompileOutput,
) -> ObligationCompileOutput:
    horizon = int(scenario.horizon_months)
    profile_count = len(tax.profile_prior_year_tax)

    configured_by_month: list[list[ScheduledObligation | RecurringObligation]] = [[] for _ in range(horizon)]
    for scheduled in scenario.scheduled_obligations:
        if 0 <= scheduled.month < horizon:
            configured_by_month[scheduled.month].append(scheduled)
    for month in range(horizon):
        configured_by_month[month].extend(
            recurring for recurring in scenario.recurring_obligations if recurring.is_active_at(month)
        )

    slot_counts: list[int] = []
    for month in range(horizon):
        tax_slots = 0
        quarter = estimated_tax_quarter(month)
        if quarter in {1, 2, 3}:
            tax_slots = sum(int(prior_year_tax > 0) for prior_year_tax in tax.profile_prior_year_tax.tolist())
        elif quarter == 4 and month // 12 - 1 >= 0:
            tax_slots = profile_count * 2
        slot_counts.append(len(configured_by_month[month]) + len(liabilities.codes) + len(properties.id) + tax_slots)
    max_slots = max(1, max(slot_counts, default=0))

    def ints(fill: int = NO_CODE) -> Int64[np.ndarray, " month obligation"]:
        return empty_month_matrix(horizon, max_slots, np.int64, fill)

    def floats(fill: float = 0.0) -> Float64[np.ndarray, " month obligation"]:
        return empty_month_matrix(horizon, max_slots, np.float64, fill)

    def bools() -> Bool[np.ndarray, " month obligation"]:
        return empty_month_matrix(horizon, max_slots, np.bool_, False)

    cause = ints()
    obligation_id = ints()
    obligation_type = ints()
    agent = ints()
    from_account = ints()
    from_slot = ints()
    to_agent = ints()
    to_account = ints()
    to_slot = ints()
    property_tax_profile = ints()
    property_slot_matrix = ints()
    deduction_profile = ints()
    deductible_fraction = floats()

    configured_active = bools()
    amount_kind = ints(AMOUNT_FIXED)
    amount_fixed = ints(0)
    amount_base = ints(0)
    amount_series = ints()
    amount_base_month = ints(0)
    amount_period = ints(1)

    property_tax_active = bools()
    property_tax_amount = ints(0)

    mortgage_active = bools()
    mortgage_liability_slot = ints(0)
    mortgage_annual_rate = floats()
    mortgage_property_purchase_month = ints(0)

    estimated_active = bools()
    estimated_quarterly = ints(0)

    q4_active = bools()
    q4_prior = ints(0)
    tax_liability_count = tax_liabilities.profile_index.shape[0]
    q4_selector = np.zeros((horizon, max_slots, tax_liability_count), dtype=np.int64)

    true_up_active = bools()
    true_up_profile = ints(0)
    true_up_prior = ints(0)
    true_up_year_end = ints(0)
    true_up_selector = np.zeros_like(q4_selector)

    agent_to_profile_index = {
        strings.require(profile.agent_id): index for index, profile in enumerate(scenario.tax_profiles)
    }

    def set_payment_metadata(
        month: int,
        slot: int,
        *,
        cause_text: str,
        type_text: str,
        agent_id: str,
        payer_account_id: str,
        payee_agent_id: str,
        payee_account_id: str,
    ) -> None:
        cause_code = strings.require(cause_text)
        cause[month, slot] = cause_code
        obligation_id[month, slot] = cause_code
        obligation_type[month, slot] = strings.require(type_text)
        agent[month, slot] = strings.require(agent_id)
        from_account[month, slot] = strings.require(payer_account_id)
        from_slot[month, slot] = account_slot_by_key.resolve(agent_id, payer_account_id)
        to_agent[month, slot] = strings.require(payee_agent_id)
        to_account[month, slot] = strings.require(payee_account_id)
        to_slot[month, slot] = account_slot_by_key.resolve(payee_agent_id, payee_account_id)

    for month in range(horizon):
        slot = 0
        for config in configured_by_month[month]:
            cause_text = f"{config.obligation_id}_m{month}"
            set_payment_metadata(
                month,
                slot,
                cause_text=cause_text,
                type_text=config.obligation_type,
                agent_id=config.agent_id,
                payer_account_id=config.from_account_id,
                payee_agent_id=config.to_agent_id,
                payee_account_id=config.to_account_id,
            )
            configured_active[month, slot] = True
            kind, fixed, base, series, base_month, period = amount_arrays_quanta(
                config.amount_due, series_index_by_id, currency_quantum=scenario.currency.quantum
            )
            amount_kind[month, slot] = kind
            amount_fixed[month, slot] = fixed
            amount_base[month, slot] = base
            amount_series[month, slot] = series
            amount_base_month[month, slot] = base_month
            amount_period[month, slot] = period
            if config.deduction_category == ORDINARY_DEDUCTION_CATEGORY:
                owner_profile = agent_to_profile_index.get(strings.require(config.agent_id), NO_CODE)
                deduction_profile[month, slot] = tax.buckets.ordinary_bucket(owner_profile)
                deductible_fraction[month, slot] = float(config.deductible_fraction)
            if config.property_id is not None:
                if config.property_id not in property_slot_by_id:
                    raise ValueError(
                        f"Obligation {config.obligation_id!r} references unknown property_id {config.property_id!r}"
                    )
                property_slot_matrix[month, slot] = property_slot_by_id[config.property_id]
            slot += 1

        for liability_slot in range(len(liabilities.codes)):
            property_slot = int(liabilities.property_slot[liability_slot])
            if property_slot < len(scenario.scheduled_property_purchases):
                purchase = scenario.scheduled_property_purchases[property_slot]
                if purchase.mortgage is not None:
                    set_payment_metadata(
                        month,
                        slot,
                        cause_text=f"{purchase.mortgage.liability_id}_payment_m{month}",
                        type_text="mortgage_payment",
                        agent_id=purchase.buyer_agent_id,
                        payer_account_id=purchase.buyer_account_id,
                        payee_agent_id=purchase.mortgage.lender_agent_id,
                        payee_account_id=purchase.mortgage.lender_account_id,
                    )
                    mortgage_active[month, slot] = True
                    mortgage_liability_slot[month, slot] = liability_slot
                    mortgage_annual_rate[month, slot] = liabilities.annual_rate[liability_slot]
                    mortgage_property_purchase_month[month, slot] = properties.month[property_slot]
                    property_slot_matrix[month, slot] = property_slot
            slot += 1

        for property_slot, _property_code in enumerate(properties.id.tolist()):
            if property_slot < len(scenario.scheduled_property_purchases):
                purchase = scenario.scheduled_property_purchases[property_slot]
                policy = next(
                    (
                        candidate
                        for candidate in scenario.property_tax_policies
                        if candidate.property_id == purchase.property_id and candidate.is_active_at(month)
                    ),
                    None,
                )
                if policy is not None:
                    set_payment_metadata(
                        month,
                        slot,
                        cause_text=f"{policy.property_id}_property_tax_m{month}",
                        type_text="property_tax",
                        agent_id=policy.owner_agent_id,
                        payer_account_id=policy.from_account_id,
                        payee_agent_id=policy.tax_authority_agent_id,
                        payee_account_id=policy.tax_authority_account_id,
                    )
                    property_tax_active[month, slot] = True
                    rate = (
                        float(policy.annual_tax_rate)
                        if policy.annual_tax_rate is not None
                        else properties.location_tax_rate[property_slot]
                    )
                    raw_amount = (
                        properties.initial_assessed_value[property_slot] * rate
                        + properties.special_assessment_annual[property_slot]
                    ) / 12.0
                    property_tax_amount[month, slot] = np.int64(
                        np.sign(raw_amount) * np.floor(np.abs(raw_amount) + 0.5)
                    )
                    owner_profile = agent_to_profile_index.get(strings.require(policy.owner_agent_id), NO_CODE)
                    property_tax_profile[month, slot] = owner_profile
                    property_slot_matrix[month, slot] = property_slot
                    if owner_profile >= 0:
                        deduction_profile[month, slot] = tax.buckets.ordinary_bucket(owner_profile)
                        deductible_fraction[month, slot] = float(purchase.rented_fraction)
            slot += 1

        quarter = estimated_tax_quarter(month)
        if quarter in {1, 2, 3}:
            for profile_index, prior_year_tax in enumerate(tax.profile_prior_year_tax.tolist()):
                if prior_year_tax <= 0:
                    continue
                profile = scenario.tax_profiles[profile_index]
                set_payment_metadata(
                    month,
                    slot,
                    cause_text=f"{profile.agent_id}_estimated_tax_q{quarter}_y{month // 12}",
                    type_text="estimated_tax",
                    agent_id=profile.agent_id,
                    payer_account_id=profile.payment_account_id,
                    payee_agent_id=profile.tax_authority_agent_id,
                    payee_account_id=profile.tax_authority_account_id,
                )
                estimated_active[month, slot] = True
                estimated_quarterly[month, slot] = np.int64(
                    np.sign(prior_year_tax / 4.0) * np.floor(np.abs(prior_year_tax / 4.0) + 0.5)
                )
                slot += 1
        elif quarter == 4:
            tax_year = month // 12 - 1
            if tax_year >= 0:
                year_end_month = tax_year * 12 + 11
                for profile_index, profile in enumerate(scenario.tax_profiles):
                    prior_year_tax = int(tax.profile_prior_year_tax[profile_index])
                    selector = (tax_liabilities.profile_index == profile_index) & (
                        tax_liabilities.year_end_month == year_end_month
                    )
                    set_payment_metadata(
                        month,
                        slot,
                        cause_text=f"{profile.agent_id}_estimated_tax_q4_y{tax_year}",
                        type_text="estimated_tax",
                        agent_id=profile.agent_id,
                        payer_account_id=profile.payment_account_id,
                        payee_agent_id=profile.tax_authority_agent_id,
                        payee_account_id=profile.tax_authority_account_id,
                    )
                    q4_active[month, slot] = True
                    q4_prior[month, slot] = prior_year_tax
                    q4_selector[month, slot] = selector
                    slot += 1

                    set_payment_metadata(
                        month,
                        slot,
                        cause_text=f"{profile.agent_id}_tax_true_up_y{tax_year}",
                        type_text="tax_true_up",
                        agent_id=profile.agent_id,
                        payer_account_id=profile.payment_account_id,
                        payee_agent_id=profile.tax_authority_agent_id,
                        payee_account_id=profile.tax_authority_account_id,
                    )
                    true_up_active[month, slot] = True
                    true_up_profile[month, slot] = profile_index
                    true_up_prior[month, slot] = prior_year_tax
                    true_up_year_end[month, slot] = year_end_month
                    true_up_selector[month, slot] = selector
                    slot += 1

    metadata = ObligationPaymentMetadata(
        cause=cause,
        id=obligation_id,
        type=obligation_type,
        agent=agent,
        from_account=from_account,
        from_slot=from_slot,
        to_agent=to_agent,
        to_account=to_account,
        to_slot=to_slot,
        property_tax_profile=property_tax_profile,
        property_slot=property_slot_matrix,
        deduction_profile=deduction_profile,
        deductible_fraction=deductible_fraction,
    )
    return ObligationCompileOutput(
        metadata=metadata,
        execution=ObligationExecution(
            metadata=ObligationMetadataExecution(
                agent=metadata.agent,
                from_slot=metadata.from_slot,
                to_slot=metadata.to_slot,
                deduction_profile=metadata.deduction_profile,
                deductible_fraction=metadata.deductible_fraction,
                property_tax_profile=metadata.property_tax_profile,
                property_slot=metadata.property_slot,
            ),
            configured=ConfiguredObligationExecution(
                active=configured_active,
                amount_kind=amount_kind,
                amount_fixed=amount_fixed,
                amount_base=amount_base,
                amount_series=amount_series,
                amount_base_month=amount_base_month,
                amount_period=amount_period,
            ),
            property_tax=PropertyTaxObligationExecution(
                property_tax_active, property_tax_amount, properties.month[np.maximum(metadata.property_slot, 0)]
            ),
            mortgage=MortgageObligationExecution(
                mortgage_active, mortgage_liability_slot, mortgage_annual_rate, mortgage_property_purchase_month
            ),
            estimated_tax=EstimatedTaxObligationExecution(estimated_active, estimated_quarterly),
            q4_estimated_tax=Q4EstimatedTaxObligationExecution(q4_active, q4_prior, q4_selector),
            tax_true_up=PriorYearTaxObligationExecution(
                true_up_active, true_up_profile, true_up_prior, true_up_selector, true_up_year_end
            ),
        ),
    )
