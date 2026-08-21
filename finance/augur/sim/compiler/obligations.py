"""Typed obligation-source compilation.

Each obligation source owns a narrow plan. The plans share one dense payment-slot
axis so the engine can merge their typed payment batches and run one common
funding/settlement operation without a source discriminator union.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

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
from finance.augur.sim.compiler.tax import TaxCompileOutput
from finance.augur.sim.scenario import RecurringObligation, Scenario, ScheduledObligation, TieredAmount


@dataclass(frozen=True)
class ObligationPaymentMetadata:
    """Wire/scatter metadata and shared settlement routing for payment slots."""

    cause: NDArray[np.int64]
    id: NDArray[np.int64]
    type: NDArray[np.int64]
    agent: NDArray[np.int64]
    from_account: NDArray[np.int64]
    from_slot: NDArray[np.int64]
    to_agent: NDArray[np.int64]
    to_account: NDArray[np.int64]
    to_slot: NDArray[np.int64]
    property_tax_profile: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    deduction_profile: NDArray[np.int64]
    deductible_fraction: NDArray[np.float64]


@dataclass(frozen=True)
class ConfiguredObligationPlan:
    active: NDArray[np.bool_]
    amount_kind: NDArray[np.int64]
    amount_fixed: NDArray[np.int64]
    amount_base: NDArray[np.int64]
    amount_series: NDArray[np.int64]
    amount_base_month: NDArray[np.int64]
    amount_period: NDArray[np.int64]
    tier_policy: NDArray[np.int64]


@dataclass(frozen=True)
class TierAmountSchedulePlan:
    value: NDArray[np.int64]
    kind: NDArray[np.int64]
    series: NDArray[np.int64]
    base_month: NDArray[np.int64]
    period: NDArray[np.int64]


@dataclass(frozen=True)
class TieredSpendingObligationPlan:
    """Static recurrent state and amount tables for tiered recurring obligations."""

    id: NDArray[np.int64]
    agent: NDArray[np.int64]
    initial_tier: NDArray[np.int64]
    tier_count: NDArray[np.int64]
    tier_id: NDArray[np.int64]
    spend: TierAmountSchedulePlan
    drop: TierAmountSchedulePlan
    recover: TierAmountSchedulePlan


@dataclass(frozen=True)
class PropertyTaxObligationPlan:
    active: NDArray[np.bool_]
    property_slot: NDArray[np.int64]
    annual_rate: NDArray[np.float64]


@dataclass(frozen=True)
class MortgageObligationPlan:
    active: NDArray[np.bool_]
    liability_slot: NDArray[np.int64]


@dataclass(frozen=True)
class EstimatedTaxObligationPlan:
    active: NDArray[np.bool_]
    profile_index: NDArray[np.int64]
    prior_year_tax: NDArray[np.int64]


@dataclass(frozen=True)
class PriorYearTaxObligationPlan:
    active: NDArray[np.bool_]
    profile_index: NDArray[np.int64]
    prior_year_tax: NDArray[np.int64]
    tax_year_end_month: NDArray[np.int64]


@dataclass(frozen=True)
class ObligationCompileOutput:
    """Common payment metadata plus one narrow plan per obligation source."""

    metadata: ObligationPaymentMetadata
    configured: ConfiguredObligationPlan
    tiered_spending: TieredSpendingObligationPlan
    property_tax: PropertyTaxObligationPlan
    mortgage: MortgageObligationPlan
    estimated_tax: EstimatedTaxObligationPlan
    q4_estimated_tax: PriorYearTaxObligationPlan
    tax_true_up: PriorYearTaxObligationPlan


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
) -> ObligationCompileOutput:
    horizon = int(scenario.horizon_months)
    profile_count = len(tax.profile_prior_year_tax)
    spending_obligations = [
        (obligation, cast(TieredAmount, obligation.amount_due))
        for obligation in scenario.recurring_obligations
        if isinstance(obligation.amount_due, TieredAmount)
    ]
    spending_policy_count = max(1, len(spending_obligations))
    max_spending_tiers = max(1, max((len(amount.tiers) for _, amount in spending_obligations), default=0))
    spending_policy_by_object = {id(obligation): index for index, (obligation, _) in enumerate(spending_obligations)}

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

    def ints(fill: int = NO_CODE) -> NDArray[np.int64]:
        return empty_month_matrix(horizon, max_slots, np.int64, fill)

    def floats(fill: float = 0.0) -> NDArray[np.float64]:
        return empty_month_matrix(horizon, max_slots, np.float64, fill)

    def bools() -> NDArray[np.bool_]:
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
    configured_tier_policy = ints()

    spending_id = np.full(spending_policy_count, NO_CODE, dtype=np.int64)
    spending_agent = np.full(spending_policy_count, NO_CODE, dtype=np.int64)
    spending_initial_tier = np.zeros(spending_policy_count, dtype=np.int64)
    spending_tier_count = np.zeros(spending_policy_count, dtype=np.int64)
    spending_tier_id = np.full((spending_policy_count, max_spending_tiers), NO_CODE, dtype=np.int64)

    def tier_ints(fill: int = 0) -> NDArray[np.int64]:
        return np.full((spending_policy_count, max_spending_tiers), fill, dtype=np.int64)

    def tier_schedule() -> TierAmountSchedulePlan:
        return TierAmountSchedulePlan(
            value=tier_ints(),
            kind=np.full(spending_policy_count, AMOUNT_FIXED, dtype=np.int64),
            series=np.full(spending_policy_count, NO_CODE, dtype=np.int64),
            base_month=np.zeros(spending_policy_count, dtype=np.int64),
            period=np.ones(spending_policy_count, dtype=np.int64),
        )

    spending_amount, spending_drop, spending_recover = tier_schedule(), tier_schedule(), tier_schedule()

    def set_tier_amount(table: TierAmountSchedulePlan, policy_index: int, tier_index: int, amount: object) -> None:
        kind, fixed, base, series, base_month, period = amount_arrays_quanta(
            amount, series_index_by_id, currency_quantum=scenario.currency.quantum
        )
        table.value[policy_index, tier_index] = fixed if kind == AMOUNT_FIXED else base
        table.kind[policy_index] = kind
        table.series[policy_index] = series
        table.base_month[policy_index] = base_month
        table.period[policy_index] = period

    for policy_index, (obligation, tiered_amount) in enumerate(spending_obligations):
        spending_id[policy_index] = strings.require(obligation.obligation_id)
        spending_agent[policy_index] = strings.require(obligation.agent_id)
        spending_tier_count[policy_index] = len(tiered_amount.tiers)
        spending_initial_tier[policy_index] = next(
            tier_index
            for tier_index, tier in enumerate(tiered_amount.tiers)
            if tier.tier_id == tiered_amount.initial_tier_id
        )
        for tier_index, tier in enumerate(tiered_amount.tiers):
            spending_tier_id[policy_index, tier_index] = strings.require(tier.tier_id)
            set_tier_amount(spending_amount, policy_index, tier_index, tier.monthly_spend)
        for boundary_index, boundary in enumerate(tiered_amount.boundaries):
            set_tier_amount(spending_drop, policy_index, boundary_index, boundary.drop_below_liquid_net_worth)
            set_tier_amount(spending_recover, policy_index, boundary_index + 1, boundary.recover_above_liquid_net_worth)

    property_tax_active = bools()
    property_tax_slot = ints(0)
    property_tax_annual_rate = floats(np.nan)

    mortgage_active = bools()
    mortgage_liability_slot = ints(0)

    estimated_active = bools()
    estimated_profile = ints(0)
    estimated_prior = ints(0)

    q4_active = bools()
    q4_profile = ints(0)
    q4_prior = ints(0)
    q4_year_end = ints(0)

    true_up_active = bools()
    true_up_profile = ints(0)
    true_up_prior = ints(0)
    true_up_year_end = ints(0)

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
            if isinstance(config.amount_due, TieredAmount):
                configured_tier_policy[month, slot] = spending_policy_by_object[id(config)]
            else:
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
                deduction_profile[month, slot] = agent_to_profile_index.get(strings.require(config.agent_id), NO_CODE)
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
                    property_tax_slot[month, slot] = property_slot
                    property_tax_annual_rate[month, slot] = (
                        float(policy.annual_tax_rate) if policy.annual_tax_rate is not None else np.nan
                    )
                    owner_profile = agent_to_profile_index.get(strings.require(policy.owner_agent_id), NO_CODE)
                    property_tax_profile[month, slot] = owner_profile
                    property_slot_matrix[month, slot] = property_slot
                    if owner_profile >= 0:
                        deduction_profile[month, slot] = owner_profile
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
                estimated_profile[month, slot] = profile_index
                estimated_prior[month, slot] = prior_year_tax
                slot += 1
        elif quarter == 4:
            tax_year = month // 12 - 1
            if tax_year >= 0:
                year_end_month = tax_year * 12 + 11
                for profile_index, profile in enumerate(scenario.tax_profiles):
                    prior_year_tax = int(tax.profile_prior_year_tax[profile_index])
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
                    q4_profile[month, slot] = profile_index
                    q4_prior[month, slot] = prior_year_tax
                    q4_year_end[month, slot] = year_end_month
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
                    slot += 1

    return ObligationCompileOutput(
        metadata=ObligationPaymentMetadata(
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
        ),
        configured=ConfiguredObligationPlan(
            active=configured_active,
            amount_kind=amount_kind,
            amount_fixed=amount_fixed,
            amount_base=amount_base,
            amount_series=amount_series,
            amount_base_month=amount_base_month,
            amount_period=amount_period,
            tier_policy=configured_tier_policy,
        ),
        tiered_spending=TieredSpendingObligationPlan(
            id=spending_id,
            agent=spending_agent,
            initial_tier=spending_initial_tier,
            tier_count=spending_tier_count,
            tier_id=spending_tier_id,
            spend=spending_amount,
            drop=spending_drop,
            recover=spending_recover,
        ),
        property_tax=PropertyTaxObligationPlan(
            active=property_tax_active, property_slot=property_tax_slot, annual_rate=property_tax_annual_rate
        ),
        mortgage=MortgageObligationPlan(active=mortgage_active, liability_slot=mortgage_liability_slot),
        estimated_tax=EstimatedTaxObligationPlan(
            active=estimated_active, profile_index=estimated_profile, prior_year_tax=estimated_prior
        ),
        q4_estimated_tax=PriorYearTaxObligationPlan(
            active=q4_active, profile_index=q4_profile, prior_year_tax=q4_prior, tax_year_end_month=q4_year_end
        ),
        tax_true_up=PriorYearTaxObligationPlan(
            active=true_up_active,
            profile_index=true_up_profile,
            prior_year_tax=true_up_prior,
            tax_year_end_month=true_up_year_end,
        ),
    )
