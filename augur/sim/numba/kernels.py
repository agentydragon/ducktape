"""Numba kernels for the dense-array simulator backend."""

from __future__ import annotations

import numpy as np
from numba import njit, prange

NO_CODE = -1
AMOUNT_FIXED = 0
AMOUNT_SERIES_INDEXED = 1
SOURCE_CONFIGURED_OBLIGATION = 0
SOURCE_MORTGAGE_PAYMENT = 1
SOURCE_PROPERTY_TAX = 2
SOURCE_ESTIMATED_TAX = 3
SOURCE_ESTIMATED_TAX_Q4 = 4
SOURCE_TAX_TRUE_UP = 5


@njit(cache=True)
def _amount_value(
    kind: int,
    fixed: float,
    base: float,
    series_index: int,
    base_month: int,
    adjustment_period: int,
    external_values: np.ndarray,
    rollout_index: int,
    month: int,
) -> float:
    if kind == AMOUNT_FIXED:
        return fixed
    elapsed = month - base_month
    reset_month = base_month + (elapsed // adjustment_period) * adjustment_period
    base_level = external_values[series_index, rollout_index, base_month]
    reset_level = external_values[series_index, rollout_index, reset_month]
    return base * reset_level / base_level


@njit(cache=True)
def _apply_brackets_one(amount: float, upper: np.ndarray, rate: np.ndarray, count: int) -> float:
    tax = 0.0
    prev_upper = 0.0
    for idx in range(count):
        slice_top = min(amount, upper[idx])
        in_bracket = max(slice_top - prev_upper, 0.0)
        tax += in_bracket * rate[idx]
        prev_upper = upper[idx]
    return tax


@njit(cache=True)
def _apply_ltcg_brackets_one(
    ltcg_amount: float, ordinary_taxable: float, upper: np.ndarray, rate: np.ndarray, count: int
) -> float:
    tax = 0.0
    prev_upper = 0.0
    total_taxable = ordinary_taxable + ltcg_amount
    for idx in range(count):
        slice_top = min(total_taxable, upper[idx])
        slice_bottom = max(ordinary_taxable, prev_upper)
        in_bracket = max(slice_top - slice_bottom, 0.0)
        tax += in_bracket * rate[idx]
        prev_upper = upper[idx]
    return tax


@njit(cache=True)
def _cash_add(cash: np.ndarray, slot: int, amount: float) -> None:
    if slot >= 0:
        cash[slot] += amount


@njit(cache=True)
def _cash_sub(cash: np.ndarray, slot: int, amount: float) -> None:
    if slot >= 0:
        cash[slot] -= amount


@njit(cache=True)
def _tax_liability_slot_for(
    tax_liability_profile_index: np.ndarray,
    tax_liability_link_index: np.ndarray,
    tax_liability_year_end_month: np.ndarray,
    profile_index: int,
    link_index: int,
    year_end_month: int,
) -> int:
    for slot in range(tax_liability_profile_index.shape[0]):
        if (
            tax_liability_profile_index[slot] == profile_index
            and tax_liability_link_index[slot] == link_index
            and tax_liability_year_end_month[slot] == year_end_month
        ):
            return slot
    return NO_CODE


@njit(cache=True)
def _actual_tax_for_profile_year(
    tax_liability_active: np.ndarray,
    tax_liability_amount: np.ndarray,
    tax_liability_profile_index: np.ndarray,
    tax_liability_year_end_month: np.ndarray,
    profile_index: int,
    year_end_month: int,
) -> float:
    total = 0.0
    for slot in range(tax_liability_amount.shape[0]):
        if (
            tax_liability_active[slot]
            and tax_liability_profile_index[slot] == profile_index
            and tax_liability_year_end_month[slot] == year_end_month
        ):
            total += tax_liability_amount[slot]
    return total


@njit(cache=True)
def _settle_tax_liabilities_for_profile_year(
    tax_liability_active: np.ndarray,
    tax_liability_amount: np.ndarray,
    tax_liability_profile_index: np.ndarray,
    tax_liability_year_end_month: np.ndarray,
    profile_index: int,
    year_end_month: int,
    settlement_amount: float,
) -> None:
    outstanding = _actual_tax_for_profile_year(
        tax_liability_active,
        tax_liability_amount,
        tax_liability_profile_index,
        tax_liability_year_end_month,
        profile_index,
        year_end_month,
    )
    if outstanding <= 0.0 or settlement_amount <= 0.0:
        return
    for slot in range(tax_liability_amount.shape[0]):
        if (
            tax_liability_active[slot]
            and tax_liability_profile_index[slot] == profile_index
            and tax_liability_year_end_month[slot] == year_end_month
        ):
            settled = min(tax_liability_amount[slot], tax_liability_amount[slot] / outstanding * settlement_amount)
            tax_liability_amount[slot] = max(0.0, tax_liability_amount[slot] - settled)


@njit(cache=True)
def _snapshot(
    month_index: int,
    rollout_index: int,
    failed: bool,
    failed_month: int,
    cash: np.ndarray,
    lot_remaining: np.ndarray,
    ordinary_ytd: np.ndarray,
    capital_gain_active: np.ndarray,
    capital_gain_ytd: np.ndarray,
    tax_liability_active: np.ndarray,
    tax_liability_amount: np.ndarray,
    property_active: np.ndarray,
    property_basis: np.ndarray,
    property_ownership: np.ndarray,
    property_contribution: np.ndarray,
    property_equity: np.ndarray,
    liability_active: np.ndarray,
    liability_principal: np.ndarray,
    liability_monthly_payment: np.ndarray,
    liability_interest_ytd: np.ndarray,
    liability_principal_ytd: np.ndarray,
    cash_state: np.ndarray,
    lot_state: np.ndarray,
    ordinary_state: np.ndarray,
    capital_gain_active_state: np.ndarray,
    capital_gain_state: np.ndarray,
    tax_liability_active_state: np.ndarray,
    tax_liability_state: np.ndarray,
    property_active_state: np.ndarray,
    property_basis_state: np.ndarray,
    property_ownership_state: np.ndarray,
    property_contribution_state: np.ndarray,
    property_equity_state: np.ndarray,
    liability_active_state: np.ndarray,
    liability_principal_state: np.ndarray,
    liability_monthly_payment_state: np.ndarray,
    liability_interest_ytd_state: np.ndarray,
    liability_principal_ytd_state: np.ndarray,
    rollout_failed_state: np.ndarray,
    rollout_failed_month_state: np.ndarray,
) -> None:
    for slot in range(cash.shape[0]):
        cash_state[month_index, rollout_index, slot] = cash[slot]
    for slot in range(lot_remaining.shape[0]):
        lot_state[month_index, rollout_index, slot] = lot_remaining[slot]
    for profile in range(ordinary_ytd.shape[0]):
        ordinary_state[month_index, rollout_index, profile] = ordinary_ytd[profile]
    for profile in range(capital_gain_ytd.shape[0]):
        for cls in range(2):
            capital_gain_active_state[month_index, rollout_index, profile, cls] = capital_gain_active[profile, cls]
            capital_gain_state[month_index, rollout_index, profile, cls] = capital_gain_ytd[profile, cls]
    for slot in range(tax_liability_amount.shape[0]):
        tax_liability_active_state[month_index, rollout_index, slot] = tax_liability_active[slot]
        tax_liability_state[month_index, rollout_index, slot] = tax_liability_amount[slot]
    for slot in range(property_active.shape[0]):
        property_active_state[month_index, rollout_index, slot] = property_active[slot]
        property_basis_state[month_index, rollout_index, slot] = property_basis[slot]
        property_ownership_state[month_index, rollout_index, slot] = property_ownership[slot]
        property_contribution_state[month_index, rollout_index, slot] = property_contribution[slot]
        property_equity_state[month_index, rollout_index, slot] = property_equity[slot]
    for slot in range(liability_active.shape[0]):
        liability_active_state[month_index, rollout_index, slot] = liability_active[slot]
        liability_principal_state[month_index, rollout_index, slot] = liability_principal[slot]
        liability_monthly_payment_state[month_index, rollout_index, slot] = liability_monthly_payment[slot]
        liability_interest_ytd_state[month_index, rollout_index, slot] = liability_interest_ytd[slot]
        liability_principal_ytd_state[month_index, rollout_index, slot] = liability_principal_ytd[slot]
    rollout_failed_state[month_index, rollout_index] = failed
    rollout_failed_month_state[month_index, rollout_index] = failed_month


@njit(cache=True)
def _zero_value_state(
    cash: np.ndarray,
    lot_remaining: np.ndarray,
    ordinary_ytd: np.ndarray,
    capital_gain_ytd: np.ndarray,
    tax_liability_amount: np.ndarray,
    property_basis: np.ndarray,
    property_ownership: np.ndarray,
    property_contribution: np.ndarray,
    property_equity: np.ndarray,
    liability_principal: np.ndarray,
    liability_monthly_payment: np.ndarray,
    liability_interest_ytd: np.ndarray,
    liability_principal_ytd: np.ndarray,
) -> None:
    for idx in range(cash.shape[0]):
        cash[idx] = 0.0
    for idx in range(lot_remaining.shape[0]):
        lot_remaining[idx] = 0.0
    for idx in range(ordinary_ytd.shape[0]):
        ordinary_ytd[idx] = 0.0
    for idx in range(capital_gain_ytd.shape[0]):
        capital_gain_ytd[idx, 0] = 0.0
        capital_gain_ytd[idx, 1] = 0.0
    for idx in range(tax_liability_amount.shape[0]):
        tax_liability_amount[idx] = 0.0
    for idx in range(property_basis.shape[0]):
        property_basis[idx] = 0.0
        property_ownership[idx] = 0.0
        property_contribution[idx] = 0.0
        property_equity[idx] = 0.0
    for idx in range(liability_principal.shape[0]):
        liability_principal[idx] = 0.0
        liability_monthly_payment[idx] = 0.0
        liability_interest_ytd[idx] = 0.0
        liability_principal_ytd[idx] = 0.0


@njit(cache=True)
def _lot_sort_less(lot_a: int, lot_b: int, lot_purchase_month: np.ndarray, lot_id_codes: np.ndarray) -> bool:
    if lot_purchase_month[lot_a] < lot_purchase_month[lot_b]:
        return True
    if lot_purchase_month[lot_a] > lot_purchase_month[lot_b]:
        return False
    return lot_id_codes[lot_a] < lot_id_codes[lot_b]


@njit(cache=True)
def _consume_lots_for_dollars(
    month: int,
    rollout_index: int,
    agent_code: int,
    asset_code: int,
    proceeds_account_slot: int,
    target_dollars: float,
    unit_price: float,
    lot_agent_codes: np.ndarray,
    lot_asset_codes: np.ndarray,
    lot_id_codes: np.ndarray,
    lot_purchase_month: np.ndarray,
    lot_cost_basis_per_unit: np.ndarray,
    lot_remaining: np.ndarray,
    cash: np.ndarray,
    capital_gain_active: np.ndarray,
    capital_gain_ytd: np.ndarray,
    capital_gain_agent_codes: np.ndarray,
    out_active: np.ndarray,
    out_units: np.ndarray,
    out_basis: np.ndarray,
    out_proceeds: np.ndarray,
    out_policy: int,
    out_asset_idx: int,
) -> float:
    if target_dollars <= 0.0 or unit_price <= 0.0:
        return 0.0
    realized = 0.0
    while realized < target_dollars - 1e-9:
        best_lot = NO_CODE
        for lot in range(lot_remaining.shape[0]):
            if lot_agent_codes[lot] != agent_code or lot_asset_codes[lot] != asset_code:
                continue
            if lot_remaining[lot] <= 0.0:
                continue
            if best_lot < 0 or _lot_sort_less(lot, best_lot, lot_purchase_month, lot_id_codes):
                best_lot = lot
        if best_lot < 0:
            break
        dollars_needed = target_dollars - realized
        units = min(lot_remaining[best_lot], dollars_needed / unit_price)
        if units <= 0.0:
            break
        proceeds = units * unit_price
        basis = units * lot_cost_basis_per_unit[best_lot]
        lot_remaining[best_lot] -= units
        _cash_add(cash, proceeds_account_slot, proceeds)
        gain = proceeds - basis
        for profile in range(capital_gain_agent_codes.shape[0]):
            if capital_gain_agent_codes[profile] == agent_code:
                cls = 0 if month - lot_purchase_month[best_lot] >= 12 else 1
                capital_gain_active[profile, cls] = True
                capital_gain_ytd[profile, cls] += gain
        out_active[month, out_policy, out_asset_idx, best_lot, rollout_index] = True
        out_units[month, out_policy, out_asset_idx, best_lot, rollout_index] += units
        out_basis[month, out_policy, out_asset_idx, best_lot, rollout_index] += basis
        out_proceeds[month, out_policy, out_asset_idx, best_lot, rollout_index] += proceeds
        realized += proceeds
    return realized


@njit(cache=True)
def _consume_lots_for_units(
    month: int,
    rollout_index: int,
    agent_code: int,
    asset_code: int,
    proceeds_account_slot: int,
    target_units: float,
    unit_price: float,
    lot_agent_codes: np.ndarray,
    lot_asset_codes: np.ndarray,
    lot_id_codes: np.ndarray,
    lot_purchase_month: np.ndarray,
    lot_cost_basis_per_unit: np.ndarray,
    lot_remaining: np.ndarray,
    cash: np.ndarray,
    capital_gain_active: np.ndarray,
    capital_gain_ytd: np.ndarray,
    capital_gain_agent_codes: np.ndarray,
    out_active: np.ndarray,
    out_units: np.ndarray,
    out_basis: np.ndarray,
    out_proceeds: np.ndarray,
    out_sale: int,
) -> None:
    remaining_units = target_units
    while remaining_units > 1e-9:
        best_lot = NO_CODE
        for lot in range(lot_remaining.shape[0]):
            if lot_agent_codes[lot] != agent_code or lot_asset_codes[lot] != asset_code:
                continue
            if lot_remaining[lot] <= 0.0:
                continue
            if best_lot < 0 or _lot_sort_less(lot, best_lot, lot_purchase_month, lot_id_codes):
                best_lot = lot
        if best_lot < 0:
            break
        units = min(lot_remaining[best_lot], remaining_units)
        proceeds = units * unit_price
        basis = units * lot_cost_basis_per_unit[best_lot]
        lot_remaining[best_lot] -= units
        _cash_add(cash, proceeds_account_slot, proceeds)
        gain = proceeds - basis
        for profile in range(capital_gain_agent_codes.shape[0]):
            if capital_gain_agent_codes[profile] == agent_code:
                cls = 0 if month - lot_purchase_month[best_lot] >= 12 else 1
                capital_gain_active[profile, cls] = True
                capital_gain_ytd[profile, cls] += gain
        out_active[month, out_sale, best_lot, rollout_index] = True
        out_units[month, out_sale, best_lot, rollout_index] += units
        out_basis[month, out_sale, best_lot, rollout_index] += basis
        out_proceeds[month, out_sale, best_lot, rollout_index] += proceeds
        remaining_units -= units


@njit(cache=True, parallel=True)
def run_simulation_kernel(
    horizon_months: int,
    rollout_count: int,
    external_values: np.ndarray,
    cash_initial_balance: np.ndarray,
    lot_id_codes: np.ndarray,
    lot_agent_codes: np.ndarray,
    lot_asset_codes: np.ndarray,
    lot_purchase_month: np.ndarray,
    lot_cost_basis_per_unit: np.ndarray,
    lot_initial_quantity: np.ndarray,
    tax_profile_agent_codes: np.ndarray,
    tax_profile_prior_year_tax: np.ndarray,
    capital_gain_agent_codes: np.ndarray,
    tax_profile_capital_gain_index: np.ndarray,
    tax_link_profile_index: np.ndarray,
    tax_link_standard_deduction: np.ndarray,
    tax_link_has_ltcg: np.ndarray,
    tax_link_ordinary_upper: np.ndarray,
    tax_link_ordinary_rate: np.ndarray,
    tax_link_ordinary_count: np.ndarray,
    tax_link_ltcg_upper: np.ndarray,
    tax_link_ltcg_rate: np.ndarray,
    tax_link_ltcg_count: np.ndarray,
    tax_liability_profile_index: np.ndarray,
    tax_liability_link_index: np.ndarray,
    tax_liability_year_end_month: np.ndarray,
    transfer_cause_codes: np.ndarray,
    transfer_from_cash_slot: np.ndarray,
    transfer_to_cash_slot: np.ndarray,
    transfer_income_profile_index: np.ndarray,
    transfer_amount_kind: np.ndarray,
    transfer_amount_fixed: np.ndarray,
    transfer_amount_base: np.ndarray,
    transfer_amount_series_index: np.ndarray,
    transfer_amount_base_month: np.ndarray,
    transfer_amount_adjustment_period: np.ndarray,
    property_cause_codes: np.ndarray,
    property_month: np.ndarray,
    property_location_tax_rate: np.ndarray,
    property_buyer_cash_slot: np.ndarray,
    property_seller_cash_slot: np.ndarray,
    property_down_payment: np.ndarray,
    property_adjusted_basis: np.ndarray,
    property_ownership_pct: np.ndarray,
    property_stake_contribution: np.ndarray,
    property_equity_ledger: np.ndarray,
    property_mortgage_slot: np.ndarray,
    liability_property_slot: np.ndarray,
    liability_payment_cash_slot: np.ndarray,
    liability_counterparty_cash_slot: np.ndarray,
    liability_principal_initial: np.ndarray,
    liability_annual_rate: np.ndarray,
    liability_term_months: np.ndarray,
    liability_monthly_payment_initial: np.ndarray,
    sale_cause_codes: np.ndarray,
    sale_month: np.ndarray,
    sale_agent_codes: np.ndarray,
    sale_asset_codes: np.ndarray,
    sale_quantity: np.ndarray,
    sale_proceeds_cash_slot: np.ndarray,
    sale_price_fixed: np.ndarray,
    sale_price_series_index: np.ndarray,
    obligation_cause_codes: np.ndarray,
    obligation_agent_codes: np.ndarray,
    obligation_from_cash_slot: np.ndarray,
    obligation_to_cash_slot: np.ndarray,
    obligation_amount_kind: np.ndarray,
    obligation_amount_fixed: np.ndarray,
    obligation_amount_base: np.ndarray,
    obligation_amount_series_index: np.ndarray,
    obligation_amount_base_month: np.ndarray,
    obligation_amount_adjustment_period: np.ndarray,
    obligation_source_kind: np.ndarray,
    obligation_source_index: np.ndarray,
    tax_settlement_profile_index: np.ndarray,
    liquidity_policy_agent_codes: np.ndarray,
    liquidity_policy_cash_slot: np.ndarray,
    liquidity_policy_buffer_trigger: np.ndarray,
    liquidity_policy_buffer_sale: np.ndarray,
    liquidity_policy_asset_codes: np.ndarray,
    liquidity_policy_asset_series_index: np.ndarray,
    cash_state: np.ndarray,
    lot_state: np.ndarray,
    ordinary_state: np.ndarray,
    capital_gain_active_state: np.ndarray,
    capital_gain_state: np.ndarray,
    tax_liability_active_state: np.ndarray,
    tax_liability_state: np.ndarray,
    property_active_state: np.ndarray,
    property_basis_state: np.ndarray,
    property_ownership_state: np.ndarray,
    property_contribution_state: np.ndarray,
    property_equity_state: np.ndarray,
    liability_active_state: np.ndarray,
    liability_principal_state: np.ndarray,
    liability_monthly_payment_state: np.ndarray,
    liability_interest_ytd_state: np.ndarray,
    liability_principal_ytd_state: np.ndarray,
    rollout_failed_state: np.ndarray,
    rollout_failed_month_state: np.ndarray,
    transfer_active: np.ndarray,
    transfer_amount: np.ndarray,
    property_transfer_active: np.ndarray,
    property_purchase_active: np.ndarray,
    mortgage_origination_active: np.ndarray,
    sched_disp_active: np.ndarray,
    sched_disp_units: np.ndarray,
    sched_disp_basis: np.ndarray,
    sched_disp_proceeds: np.ndarray,
    liq_disp_active: np.ndarray,
    liq_disp_units: np.ndarray,
    liq_disp_basis: np.ndarray,
    liq_disp_proceeds: np.ndarray,
    tax_accrual_active: np.ndarray,
    tax_accrual_amount: np.ndarray,
    tax_breakdown_ordinary: np.ndarray,
    tax_breakdown_ltcg: np.ndarray,
    tax_breakdown_stcg: np.ndarray,
    tax_breakdown_ordinary_taxable: np.ndarray,
    tax_breakdown_capital_taxable: np.ndarray,
    tax_breakdown_ordinary_tax: np.ndarray,
    tax_breakdown_capital_tax: np.ndarray,
    obligation_active: np.ndarray,
    obligation_due: np.ndarray,
    obligation_paid: np.ndarray,
    obligation_shortfall: np.ndarray,
    obligation_attempt_policy: np.ndarray,
    obligation_failure_active: np.ndarray,
    mortgage_payment_active: np.ndarray,
    mortgage_payment_interest: np.ndarray,
    mortgage_payment_principal: np.ndarray,
    mortgage_payment_total: np.ndarray,
    tax_settlement_active: np.ndarray,
    tax_settlement_amount: np.ndarray,
    tax_settlement_year_end_month: np.ndarray,
) -> None:
    cash_count = cash_initial_balance.shape[0]
    lot_count = lot_initial_quantity.shape[0]
    profile_count = tax_profile_agent_codes.shape[0]
    capital_gain_agent_count = capital_gain_agent_codes.shape[0]
    tax_liability_count = tax_liability_profile_index.shape[0]
    property_count = property_month.shape[0]
    liability_count = liability_principal_initial.shape[0]

    for rollout_index in prange(rollout_count):
        cash = np.empty(cash_count, dtype=np.float64)
        for idx in range(cash_count):
            cash[idx] = cash_initial_balance[idx]
        lot_remaining = np.empty(lot_count, dtype=np.float64)
        for idx in range(lot_count):
            lot_remaining[idx] = lot_initial_quantity[idx]
        ordinary_ytd = np.zeros(profile_count, dtype=np.float64)
        capital_gain_active = np.zeros((capital_gain_agent_count, 2), dtype=np.bool_)
        capital_gain_ytd = np.zeros((capital_gain_agent_count, 2), dtype=np.float64)
        tax_liability_active = np.zeros(tax_liability_count, dtype=np.bool_)
        tax_liability_amount = np.zeros(tax_liability_count, dtype=np.float64)
        property_active = np.zeros(property_count, dtype=np.bool_)
        property_basis = np.zeros(property_count, dtype=np.float64)
        property_ownership = np.zeros(property_count, dtype=np.float64)
        property_contribution = np.zeros(property_count, dtype=np.float64)
        property_equity = np.zeros(property_count, dtype=np.float64)
        liability_active = np.zeros(liability_count, dtype=np.bool_)
        liability_principal = np.zeros(liability_count, dtype=np.float64)
        liability_monthly_payment = np.zeros(liability_count, dtype=np.float64)
        liability_interest_ytd = np.zeros(liability_count, dtype=np.float64)
        liability_principal_ytd = np.zeros(liability_count, dtype=np.float64)
        failed = False
        failed_month = NO_CODE

        _snapshot(
            0,
            rollout_index,
            failed,
            failed_month,
            cash,
            lot_remaining,
            ordinary_ytd,
            capital_gain_active,
            capital_gain_ytd,
            tax_liability_active,
            tax_liability_amount,
            property_active,
            property_basis,
            property_ownership,
            property_contribution,
            property_equity,
            liability_active,
            liability_principal,
            liability_monthly_payment,
            liability_interest_ytd,
            liability_principal_ytd,
            cash_state,
            lot_state,
            ordinary_state,
            capital_gain_active_state,
            capital_gain_state,
            tax_liability_active_state,
            tax_liability_state,
            property_active_state,
            property_basis_state,
            property_ownership_state,
            property_contribution_state,
            property_equity_state,
            liability_active_state,
            liability_principal_state,
            liability_monthly_payment_state,
            liability_interest_ytd_state,
            liability_principal_ytd_state,
            rollout_failed_state,
            rollout_failed_month_state,
        )

        for month in range(horizon_months):
            # Scheduled / recurring transfers.
            for slot in range(transfer_cause_codes.shape[1]):
                if transfer_cause_codes[month, slot] < 0:
                    continue
                amount = _amount_value(
                    transfer_amount_kind[month, slot],
                    transfer_amount_fixed[month, slot],
                    transfer_amount_base[month, slot],
                    transfer_amount_series_index[month, slot],
                    transfer_amount_base_month[month, slot],
                    transfer_amount_adjustment_period[month, slot],
                    external_values,
                    rollout_index,
                    month,
                )
                transfer_active[month, slot, rollout_index] = True
                transfer_amount[month, slot, rollout_index] = amount
                _cash_sub(cash, transfer_from_cash_slot[month, slot], amount)
                _cash_add(cash, transfer_to_cash_slot[month, slot], amount)
                profile = transfer_income_profile_index[month, slot]
                if profile >= 0:
                    ordinary_ytd[profile] += amount

            # Real-estate purchase state and buyer cash transfer.
            for prop in range(property_count):
                if property_month[prop] != month:
                    continue
                property_purchase_active[month, prop, rollout_index] = True
                property_active[prop] = True
                property_basis[prop] = property_adjusted_basis[prop]
                property_ownership[prop] = property_ownership_pct[prop]
                property_contribution[prop] = property_stake_contribution[prop]
                property_equity[prop] = property_equity_ledger[prop]
                buyer_cash = property_stake_contribution[prop]
                if buyer_cash > 0.0:
                    property_transfer_active[month, prop, rollout_index] = True
                    _cash_sub(cash, property_buyer_cash_slot[prop], buyer_cash)
                    _cash_add(cash, property_seller_cash_slot[prop], buyer_cash)
                liability_slot = property_mortgage_slot[prop]
                if liability_slot >= 0:
                    mortgage_origination_active[month, liability_slot, rollout_index] = True
                    liability_active[liability_slot] = True
                    liability_principal[liability_slot] = liability_principal_initial[liability_slot]
                    liability_monthly_payment[liability_slot] = liability_monthly_payment_initial[liability_slot]
                    liability_interest_ytd[liability_slot] = 0.0
                    liability_principal_ytd[liability_slot] = 0.0

            # Scheduled asset sales.
            for sale in range(sale_month.shape[0]):
                if sale_month[sale] != month:
                    continue
                price = sale_price_fixed[sale]
                if np.isnan(price):
                    price = external_values[sale_price_series_index[sale], rollout_index, month]
                _consume_lots_for_units(
                    month,
                    rollout_index,
                    sale_agent_codes[sale],
                    sale_asset_codes[sale],
                    sale_proceeds_cash_slot[sale],
                    sale_quantity[sale],
                    price,
                    lot_agent_codes,
                    lot_asset_codes,
                    lot_id_codes,
                    lot_purchase_month,
                    lot_cost_basis_per_unit,
                    lot_remaining,
                    cash,
                    capital_gain_active,
                    capital_gain_ytd,
                    capital_gain_agent_codes,
                    sched_disp_active,
                    sched_disp_units,
                    sched_disp_basis,
                    sched_disp_proceeds,
                    sale,
                )

            # Year-end tax accruals, computed after scheduled events but before policy events.
            if month % 12 == 11:
                for link in range(tax_link_profile_index.shape[0]):
                    profile = tax_link_profile_index[link]
                    gain_profile = tax_profile_capital_gain_index[profile]
                    ordinary = ordinary_ytd[profile]
                    ltcg = capital_gain_ytd[gain_profile, 0]
                    stcg = capital_gain_ytd[gain_profile, 1]
                    deduction = tax_link_standard_deduction[link]
                    if tax_link_has_ltcg[link] == 1:
                        ordinary_taxable = max(ordinary + stcg - deduction, 0.0)
                        capital_taxable = ltcg
                        ordinary_tax = _apply_brackets_one(
                            ordinary_taxable,
                            tax_link_ordinary_upper[link],
                            tax_link_ordinary_rate[link],
                            tax_link_ordinary_count[link],
                        )
                        capital_tax = _apply_ltcg_brackets_one(
                            ltcg,
                            ordinary_taxable,
                            tax_link_ltcg_upper[link],
                            tax_link_ltcg_rate[link],
                            tax_link_ltcg_count[link],
                        )
                    else:
                        ordinary_taxable = max(ordinary + ltcg + stcg - deduction, 0.0)
                        capital_taxable = 0.0
                        ordinary_tax = _apply_brackets_one(
                            ordinary_taxable,
                            tax_link_ordinary_upper[link],
                            tax_link_ordinary_rate[link],
                            tax_link_ordinary_count[link],
                        )
                        capital_tax = 0.0
                    tax = ordinary_tax + capital_tax
                    tax_accrual_active[month, link, rollout_index] = True
                    tax_accrual_amount[month, link, rollout_index] = tax
                    tax_breakdown_ordinary[month, link, rollout_index] = ordinary
                    tax_breakdown_ltcg[month, link, rollout_index] = ltcg
                    tax_breakdown_stcg[month, link, rollout_index] = stcg
                    tax_breakdown_ordinary_taxable[month, link, rollout_index] = ordinary_taxable
                    tax_breakdown_capital_taxable[month, link, rollout_index] = capital_taxable
                    tax_breakdown_ordinary_tax[month, link, rollout_index] = ordinary_tax
                    tax_breakdown_capital_tax[month, link, rollout_index] = capital_tax
                    tax_slot = _tax_liability_slot_for(
                        tax_liability_profile_index,
                        tax_liability_link_index,
                        tax_liability_year_end_month,
                        profile,
                        link,
                        month,
                    )
                    if tax_slot >= 0:
                        tax_liability_active[tax_slot] = True
                        tax_liability_amount[tax_slot] = tax
                for profile in range(profile_count):
                    ordinary_ytd[profile] = 0.0
                    gain_profile = tax_profile_capital_gain_index[profile]
                    if capital_gain_active[gain_profile, 0]:
                        capital_gain_ytd[gain_profile, 0] = 0.0
                    if capital_gain_active[gain_profile, 1]:
                        capital_gain_ytd[gain_profile, 1] = 0.0

            if not failed:
                obligation_amount = np.zeros(obligation_cause_codes.shape[1], dtype=np.float64)
                obligation_is_active = np.zeros(obligation_cause_codes.shape[1], dtype=np.bool_)
                tax_settlement_candidate = np.zeros(max(1, profile_count), dtype=np.float64)
                tax_settlement_candidate_year_end = np.full(max(1, profile_count), NO_CODE, dtype=np.int64)

                # Accrue hard demands.
                for slot in range(obligation_cause_codes.shape[1]):
                    if obligation_cause_codes[month, slot] < 0 or obligation_source_kind[month, slot] < 0:
                        continue
                    source_kind = obligation_source_kind[month, slot]
                    source_index = obligation_source_index[month, slot]
                    amount = 0.0
                    active = True
                    if source_kind == SOURCE_CONFIGURED_OBLIGATION:
                        amount = _amount_value(
                            obligation_amount_kind[month, slot],
                            obligation_amount_fixed[month, slot],
                            obligation_amount_base[month, slot],
                            obligation_amount_series_index[month, slot],
                            obligation_amount_base_month[month, slot],
                            obligation_amount_adjustment_period[month, slot],
                            external_values,
                            rollout_index,
                            month,
                        )
                    elif source_kind == SOURCE_MORTGAGE_PAYMENT:
                        liab = source_index
                        prop = liability_property_slot[liab]
                        active = (
                            liability_active[liab] and property_month[prop] < month and liability_principal[liab] > 0.0
                        )
                        if active:
                            interest = liability_principal[liab] * liability_annual_rate[liab] / 12.0
                            amount = min(liability_monthly_payment[liab], liability_principal[liab] + interest)
                    elif source_kind == SOURCE_PROPERTY_TAX:
                        prop = source_index
                        active = property_active[prop] and property_month[prop] < month
                        if active:
                            rate = obligation_amount_fixed[month, slot]
                            if np.isnan(rate):
                                rate = property_location_tax_rate[prop]
                            amount = property_basis[prop] * rate / 12.0
                    elif source_kind == SOURCE_ESTIMATED_TAX:
                        amount = tax_profile_prior_year_tax[source_index] / 4.0
                    elif source_kind in (SOURCE_ESTIMATED_TAX_Q4, SOURCE_TAX_TRUE_UP):
                        profile = source_index
                        tax_year_end = (month // 12 - 1) * 12 + 11
                        actual = _actual_tax_for_profile_year(
                            tax_liability_active,
                            tax_liability_amount,
                            tax_liability_profile_index,
                            tax_liability_year_end_month,
                            profile,
                            tax_year_end,
                        )
                        safe_harbor = min(tax_profile_prior_year_tax[profile], actual)
                        paid_before_q4 = tax_profile_prior_year_tax[profile] * 0.75
                        if source_kind == SOURCE_ESTIMATED_TAX_Q4:
                            amount = max(0.0, safe_harbor - paid_before_q4)
                        else:
                            amount = max(0.0, actual - safe_harbor)
                            tax_settlement_candidate[profile] = actual
                            tax_settlement_candidate_year_end[profile] = tax_year_end
                    if active and amount > 0.0:
                        obligation_is_active[slot] = True
                        obligation_amount[slot] = amount
                        obligation_active[month, slot, rollout_index] = True
                        obligation_due[month, slot, rollout_index] = amount

                # Liquidity policies plan sales before settlement.
                for policy in range(liquidity_policy_agent_codes.shape[0]):
                    policy_cash_slot = liquidity_policy_cash_slot[policy]
                    policy_agent = liquidity_policy_agent_codes[policy]
                    if policy_cash_slot < 0 and cash_count > 0:
                        cash_balance = 0.0
                    else:
                        cash_balance = cash[policy_cash_slot] if policy_cash_slot >= 0 else 0.0
                    hard_demand = 0.0
                    for slot in range(obligation_is_active.shape[0]):
                        if not obligation_is_active[slot]:
                            continue
                        if (
                            obligation_agent_codes[month, slot] == policy_agent
                            and obligation_from_cash_slot[month, slot] == policy_cash_slot
                        ):
                            hard_demand += obligation_amount[slot]
                            obligation_attempt_policy[month, slot, rollout_index] = policy
                    required_sale = max(0.0, hard_demand - cash_balance)
                    post_required_cash = cash_balance + required_sale - hard_demand
                    buffer_sale = 0.0
                    if (
                        liquidity_policy_buffer_sale[policy] > 0.0
                        and post_required_cash < liquidity_policy_buffer_trigger[policy]
                    ):
                        buffer_sale = liquidity_policy_buffer_sale[policy]
                    remaining_deficit = required_sale + buffer_sale
                    if hard_demand <= 0.0 and remaining_deficit <= 0.0:
                        continue
                    for asset_idx in range(liquidity_policy_asset_codes.shape[1]):
                        asset_code = liquidity_policy_asset_codes[policy, asset_idx]
                        if asset_code < 0 or remaining_deficit <= 0.0:
                            continue
                        series_index = liquidity_policy_asset_series_index[policy, asset_idx]
                        if series_index < 0:
                            continue
                        unit_price = external_values[series_index, rollout_index, month]
                        if np.isnan(unit_price) or unit_price <= 0.0:
                            continue
                        realized = _consume_lots_for_dollars(
                            month,
                            rollout_index,
                            policy_agent,
                            asset_code,
                            policy_cash_slot,
                            remaining_deficit,
                            unit_price,
                            lot_agent_codes,
                            lot_asset_codes,
                            lot_id_codes,
                            lot_purchase_month,
                            lot_cost_basis_per_unit,
                            lot_remaining,
                            cash,
                            capital_gain_active,
                            capital_gain_ytd,
                            capital_gain_agent_codes,
                            liq_disp_active,
                            liq_disp_units,
                            liq_disp_basis,
                            liq_disp_proceeds,
                            policy,
                            asset_idx,
                        )
                        remaining_deficit -= realized

                # Mechanical settlement, grouped by account.
                obligation_group_funded = np.zeros(obligation_is_active.shape[0], dtype=np.bool_)
                for slot in range(obligation_is_active.shape[0]):
                    if not obligation_is_active[slot]:
                        continue
                    agent = obligation_agent_codes[month, slot]
                    from_slot = obligation_from_cash_slot[month, slot]
                    total_due = 0.0
                    for other in range(obligation_is_active.shape[0]):
                        if (
                            obligation_is_active[other]
                            and obligation_agent_codes[month, other] == agent
                            and obligation_from_cash_slot[month, other] == from_slot
                        ):
                            total_due += obligation_amount[other]
                    available = cash[from_slot] if from_slot >= 0 else 0.0
                    obligation_group_funded[slot] = available >= total_due - 1e-9

                tax_payment_failed = np.zeros(max(1, profile_count), dtype=np.bool_)
                for slot in range(obligation_is_active.shape[0]):
                    if not obligation_is_active[slot]:
                        continue
                    if obligation_group_funded[slot]:
                        amount = obligation_amount[slot]
                        obligation_paid[month, slot, rollout_index] = amount
                        from_slot = obligation_from_cash_slot[month, slot]
                        _cash_sub(cash, from_slot, amount)
                        _cash_add(cash, obligation_to_cash_slot[month, slot], amount)
                        source_kind = obligation_source_kind[month, slot]
                        source_index = obligation_source_index[month, slot]
                        if source_kind == SOURCE_MORTGAGE_PAYMENT:
                            liab = source_index
                            interest = min(liability_principal[liab] * liability_annual_rate[liab] / 12.0, amount)
                            principal = max(0.0, amount - interest)
                            principal = min(principal, liability_principal[liab])
                            mortgage_payment_active[month, liab, rollout_index] = True
                            mortgage_payment_interest[month, liab, rollout_index] = interest
                            mortgage_payment_principal[month, liab, rollout_index] = principal
                            mortgage_payment_total[month, liab, rollout_index] = amount
                            liability_principal[liab] = max(0.0, liability_principal[liab] - principal)
                            liability_interest_ytd[liab] += interest
                            liability_principal_ytd[liab] += principal
                    else:
                        amount = obligation_amount[slot]
                        obligation_shortfall[month, slot, rollout_index] = amount
                        obligation_failure_active[month, slot, rollout_index] = True
                        failed = True
                        if failed_month < 0:
                            failed_month = month
                        source_kind = obligation_source_kind[month, slot]
                        if source_kind in (SOURCE_ESTIMATED_TAX, SOURCE_ESTIMATED_TAX_Q4, SOURCE_TAX_TRUE_UP):
                            profile = obligation_source_index[month, slot]
                            tax_payment_failed[profile] = True

                for profile in range(profile_count):
                    if tax_settlement_candidate[profile] > 0.0 and not tax_payment_failed[profile]:
                        tax_settlement_active[month, profile, rollout_index] = True
                        tax_settlement_amount[month, profile, rollout_index] = tax_settlement_candidate[profile]
                        tax_settlement_year_end_month[month, profile, rollout_index] = (
                            tax_settlement_candidate_year_end[profile]
                        )
                        _settle_tax_liabilities_for_profile_year(
                            tax_liability_active,
                            tax_liability_amount,
                            tax_liability_profile_index,
                            tax_liability_year_end_month,
                            profile,
                            tax_settlement_candidate_year_end[profile],
                            tax_settlement_candidate[profile],
                        )

            if failed:
                _zero_value_state(
                    cash,
                    lot_remaining,
                    ordinary_ytd,
                    capital_gain_ytd,
                    tax_liability_amount,
                    property_basis,
                    property_ownership,
                    property_contribution,
                    property_equity,
                    liability_principal,
                    liability_monthly_payment,
                    liability_interest_ytd,
                    liability_principal_ytd,
                )

            _snapshot(
                month + 1,
                rollout_index,
                failed,
                failed_month,
                cash,
                lot_remaining,
                ordinary_ytd,
                capital_gain_active,
                capital_gain_ytd,
                tax_liability_active,
                tax_liability_amount,
                property_active,
                property_basis,
                property_ownership,
                property_contribution,
                property_equity,
                liability_active,
                liability_principal,
                liability_monthly_payment,
                liability_interest_ytd,
                liability_principal_ytd,
                cash_state,
                lot_state,
                ordinary_state,
                capital_gain_active_state,
                capital_gain_state,
                tax_liability_active_state,
                tax_liability_state,
                property_active_state,
                property_basis_state,
                property_ownership_state,
                property_contribution_state,
                property_equity_state,
                liability_active_state,
                liability_principal_state,
                liability_monthly_payment_state,
                liability_interest_ytd_state,
                liability_principal_ytd_state,
                rollout_failed_state,
                rollout_failed_month_state,
            )
