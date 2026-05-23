"""Python boundary for the dense-array Numba simulator.

This module owns all object-heavy work: string interning, Pydantic scenario
inspection, Polars external-series reshaping, and static event-slot planning.
The kernel consumes only numeric arrays and writes numeric outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from augur.sim.external_series import ExternalSeriesContext
from augur.sim.jurisdictions import Jurisdiction
from augur.sim.locations import Location
from augur.sim.runtime import mortgage_monthly_payment_usd
from augur.sim.scenario import (
    FixedAmount,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledObligation,
    ScheduledTransfer,
    SeriesIndexedAmount,
)

NO_CODE = -1
AMOUNT_FIXED = 0
AMOUNT_SERIES_INDEXED = 1
ORDINARY_INCOME_CATEGORY = "ordinary"


class StringTable:
    def __init__(self) -> None:
        self._by_value: dict[str, int] = {}
        self.values: list[str] = []

    def intern(self, value: str | None) -> int:
        if value is None:
            return NO_CODE
        existing = self._by_value.get(value)
        if existing is not None:
            return existing
        code = len(self.values)
        self._by_value[value] = code
        self.values.append(value)
        return code

    def require(self, value: str) -> int:
        return self.intern(value)


@dataclass(frozen=True)
class SlotPlan:
    """Dense shape contract for one compiled simulation.

    Dimensions use the notation from `augur/plans/numba_shape_discipline.md`.
    Counts that can be absent but are still iterated by the kernel use their
    allocated sentinel axis size, usually `max(1, actual_count)`.
    """

    event_months: int
    snapshot_months: int
    rollout_count: int
    cash_count: int
    lot_count: int
    tax_profile_count: int
    capital_gain_agent_count: int
    tax_link_count: int
    tax_liability_count: int
    property_count: int
    liability_count: int
    max_transfer_slots: int
    max_obligation_slots: int
    scheduled_sale_count: int
    liquidity_policy_count: int
    max_liquidity_policy_assets: int
    max_tax_settlement_slots: int


@dataclass(frozen=True)
class CompiledSimulation:
    horizon_months: int
    rollout_count: int
    slot_plan: SlotPlan
    strings: tuple[str, ...]
    series_ids: tuple[str, ...]
    external_values: np.ndarray
    cash_agent_codes: np.ndarray
    cash_account_codes: np.ndarray
    cash_initial_balance: np.ndarray
    lot_id_codes: np.ndarray
    lot_agent_codes: np.ndarray
    lot_asset_codes: np.ndarray
    lot_purchase_month: np.ndarray
    lot_cost_basis_per_unit: np.ndarray
    lot_initial_quantity: np.ndarray
    tax_profile_agent_codes: np.ndarray
    tax_profile_payment_slot: np.ndarray
    tax_profile_payment_account_codes: np.ndarray
    tax_profile_authority_agent_codes: np.ndarray
    tax_profile_authority_account_codes: np.ndarray
    tax_profile_prior_year_tax: np.ndarray
    capital_gain_agent_codes: np.ndarray
    tax_profile_capital_gain_index: np.ndarray
    tax_link_profile_index: np.ndarray
    tax_link_jurisdiction_codes: np.ndarray
    tax_link_standard_deduction: np.ndarray
    tax_link_has_ltcg: np.ndarray
    tax_link_ordinary_upper: np.ndarray
    tax_link_ordinary_rate: np.ndarray
    tax_link_ordinary_count: np.ndarray
    tax_link_ltcg_upper: np.ndarray
    tax_link_ltcg_rate: np.ndarray
    tax_link_ltcg_count: np.ndarray
    tax_liability_profile_index: np.ndarray
    tax_liability_link_index: np.ndarray
    tax_liability_year_end_month: np.ndarray
    transfer_cause_codes: np.ndarray
    transfer_from_agent_codes: np.ndarray
    transfer_from_account_codes: np.ndarray
    transfer_from_cash_slot: np.ndarray
    transfer_to_agent_codes: np.ndarray
    transfer_to_account_codes: np.ndarray
    transfer_to_cash_slot: np.ndarray
    transfer_income_profile_index: np.ndarray
    transfer_amount_kind: np.ndarray
    transfer_amount_fixed: np.ndarray
    transfer_amount_base: np.ndarray
    transfer_amount_series_index: np.ndarray
    transfer_amount_base_month: np.ndarray
    transfer_amount_adjustment_period: np.ndarray
    property_cause_codes: np.ndarray
    property_id_codes: np.ndarray
    property_location_codes: np.ndarray
    property_location_tax_rate: np.ndarray
    property_month: np.ndarray
    property_buyer_agent_codes: np.ndarray
    property_buyer_account_codes: np.ndarray
    property_buyer_cash_slot: np.ndarray
    property_seller_agent_codes: np.ndarray
    property_seller_account_codes: np.ndarray
    property_seller_cash_slot: np.ndarray
    property_purchase_price: np.ndarray
    property_closing_cost: np.ndarray
    property_down_payment: np.ndarray
    property_adjusted_basis: np.ndarray
    property_ownership_pct: np.ndarray
    property_stake_contribution: np.ndarray
    property_equity_ledger: np.ndarray
    property_mortgage_slot: np.ndarray
    liability_codes: np.ndarray
    liability_property_slot: np.ndarray
    liability_agent_codes: np.ndarray
    liability_payment_account_codes: np.ndarray
    liability_payment_cash_slot: np.ndarray
    liability_counterparty_agent_codes: np.ndarray
    liability_counterparty_account_codes: np.ndarray
    liability_counterparty_cash_slot: np.ndarray
    liability_principal: np.ndarray
    liability_annual_rate: np.ndarray
    liability_term_months: np.ndarray
    liability_monthly_payment: np.ndarray
    sale_cause_codes: np.ndarray
    sale_month: np.ndarray
    sale_agent_codes: np.ndarray
    sale_asset_codes: np.ndarray
    sale_quantity: np.ndarray
    sale_proceeds_account_codes: np.ndarray
    sale_proceeds_cash_slot: np.ndarray
    sale_price_fixed: np.ndarray
    sale_price_series_index: np.ndarray
    obligation_cause_codes: np.ndarray
    obligation_id_codes: np.ndarray
    obligation_type_codes: np.ndarray
    obligation_agent_codes: np.ndarray
    obligation_from_account_codes: np.ndarray
    obligation_from_cash_slot: np.ndarray
    obligation_to_agent_codes: np.ndarray
    obligation_to_account_codes: np.ndarray
    obligation_to_cash_slot: np.ndarray
    obligation_amount_kind: np.ndarray
    obligation_amount_fixed: np.ndarray
    obligation_amount_base: np.ndarray
    obligation_amount_series_index: np.ndarray
    obligation_amount_base_month: np.ndarray
    obligation_amount_adjustment_period: np.ndarray
    obligation_source_kind: np.ndarray
    obligation_source_index: np.ndarray
    tax_settlement_profile_index: np.ndarray
    liquidity_policy_agent_codes: np.ndarray
    liquidity_policy_account_codes: np.ndarray
    liquidity_policy_cash_slot: np.ndarray
    liquidity_policy_buffer_trigger: np.ndarray
    liquidity_policy_buffer_sale: np.ndarray
    liquidity_policy_asset_codes: np.ndarray
    liquidity_policy_asset_series_index: np.ndarray
    liquidity_policy_prefixes: tuple[str, ...]


def compile_simulation(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    jurisdictions: dict[str, Jurisdiction],
    locations: dict[str, Location],
) -> CompiledSimulation:
    strings = StringTable()
    horizon = int(scenario.horizon_months)

    account_slot_by_key: dict[tuple[str, str], int] = {}
    cash_agent_codes: list[int] = []
    cash_account_codes: list[int] = []
    cash_initial_balance: list[float] = []
    for entry in scenario.initial_cash:
        key = (entry.agent_id, entry.account_id)
        if key in account_slot_by_key:
            raise ValueError(f"duplicate initial cash account: {entry.agent_id}/{entry.account_id}")
        account_slot_by_key[key] = len(cash_initial_balance)
        cash_agent_codes.append(strings.require(entry.agent_id))
        cash_account_codes.append(strings.require(entry.account_id))
        cash_initial_balance.append(float(entry.balance_usd))

    for agent in scenario.agents:
        strings.require(agent.agent_id)

    series_ids = _collect_series_ids(scenario, external_series)
    series_index_by_id = {series_id: idx for idx, series_id in enumerate(series_ids)}
    external_values = _external_values_cube(
        external_series, series_index_by_id=series_index_by_id, rollout_count=rollout_count, horizon_months=horizon
    )

    profile_index_by_agent = {profile.agent_id: idx for idx, profile in enumerate(scenario.tax_profiles)}
    (
        tax_profile_agent_codes,
        tax_profile_payment_slot,
        tax_profile_payment_account_codes,
        tax_profile_authority_agent_codes,
        tax_profile_authority_account_codes,
        tax_profile_prior_year_tax,
        tax_link_profile_index,
        tax_link_jurisdiction_codes,
        tax_link_standard_deduction,
        tax_link_has_ltcg,
        tax_link_ordinary_upper,
        tax_link_ordinary_rate,
        tax_link_ordinary_count,
        tax_link_ltcg_upper,
        tax_link_ltcg_rate,
        tax_link_ltcg_count,
    ) = _compile_tax(scenario, strings, account_slot_by_key, jurisdictions)
    (capital_gain_agent_codes, tax_profile_capital_gain_index) = _compile_capital_gain_agents(scenario, strings)

    (tax_liability_profile_index, tax_liability_link_index, tax_liability_year_end_month) = (
        _compile_tax_liability_slots(horizon, tax_link_profile_index)
    )

    (
        transfer_cause_codes,
        transfer_from_agent_codes,
        transfer_from_account_codes,
        transfer_from_cash_slot,
        transfer_to_agent_codes,
        transfer_to_account_codes,
        transfer_to_cash_slot,
        transfer_income_profile_index,
        transfer_amount_kind,
        transfer_amount_fixed,
        transfer_amount_base,
        transfer_amount_series_index,
        transfer_amount_base_month,
        transfer_amount_adjustment_period,
    ) = _compile_transfer_slots(scenario, strings, account_slot_by_key, profile_index_by_agent, series_index_by_id)

    (
        property_cause_codes,
        property_id_codes,
        property_location_codes,
        property_location_tax_rate,
        property_month,
        property_buyer_agent_codes,
        property_buyer_account_codes,
        property_buyer_cash_slot,
        property_seller_agent_codes,
        property_seller_account_codes,
        property_seller_cash_slot,
        property_purchase_price,
        property_closing_cost,
        property_down_payment,
        property_adjusted_basis,
        property_ownership_pct,
        property_stake_contribution,
        property_equity_ledger,
        property_mortgage_slot,
        liability_codes,
        liability_property_slot,
        liability_agent_codes,
        liability_payment_account_codes,
        liability_payment_cash_slot,
        liability_counterparty_agent_codes,
        liability_counterparty_account_codes,
        liability_counterparty_cash_slot,
        liability_principal,
        liability_annual_rate,
        liability_term_months,
        liability_monthly_payment,
    ) = _compile_properties_and_liabilities(scenario, strings, account_slot_by_key, locations)

    (
        sale_cause_codes,
        sale_month,
        sale_agent_codes,
        sale_asset_codes,
        sale_quantity,
        sale_proceeds_account_codes,
        sale_proceeds_cash_slot,
        sale_price_fixed,
        sale_price_series_index,
    ) = _compile_sales(scenario, strings, account_slot_by_key, series_index_by_id)

    (
        obligation_cause_codes,
        obligation_id_codes,
        obligation_type_codes,
        obligation_agent_codes,
        obligation_from_account_codes,
        obligation_from_cash_slot,
        obligation_to_agent_codes,
        obligation_to_account_codes,
        obligation_to_cash_slot,
        obligation_amount_kind,
        obligation_amount_fixed,
        obligation_amount_base,
        obligation_amount_series_index,
        obligation_amount_base_month,
        obligation_amount_adjustment_period,
        obligation_source_kind,
        obligation_source_index,
        tax_settlement_profile_index,
    ) = _compile_obligation_slots(
        scenario,
        strings,
        account_slot_by_key,
        series_index_by_id,
        property_id_codes,
        property_month,
        liability_codes,
        liability_property_slot,
        tax_profile_prior_year_tax,
    )

    (
        liquidity_policy_agent_codes,
        liquidity_policy_account_codes,
        liquidity_policy_cash_slot,
        liquidity_policy_buffer_trigger,
        liquidity_policy_buffer_sale,
        liquidity_policy_asset_codes,
        liquidity_policy_asset_series_index,
        liquidity_policy_prefixes,
    ) = _compile_liquidity_policies(scenario, strings, account_slot_by_key, series_index_by_id)

    lot_id_codes = []
    lot_agent_codes = []
    lot_asset_codes = []
    lot_purchase_month = []
    lot_cost_basis_per_unit = []
    lot_initial_quantity = []
    for lot in scenario.initial_lots:
        lot_id_codes.append(strings.require(lot.lot_id))
        lot_agent_codes.append(strings.require(lot.agent_id))
        lot_asset_codes.append(strings.require(lot.asset_id))
        lot_purchase_month.append(int(lot.purchase_month_index))
        lot_cost_basis_per_unit.append(float(lot.cost_basis_per_unit_usd))
        lot_initial_quantity.append(float(lot.quantity))

    slot_plan = SlotPlan(
        event_months=horizon,
        snapshot_months=horizon + 1,
        rollout_count=rollout_count,
        cash_count=len(cash_initial_balance),
        lot_count=len(lot_id_codes),
        tax_profile_count=tax_profile_agent_codes.shape[0],
        capital_gain_agent_count=capital_gain_agent_codes.shape[0],
        tax_link_count=max(1, tax_link_profile_index.shape[0]),
        tax_liability_count=tax_liability_profile_index.shape[0],
        property_count=property_month.shape[0],
        liability_count=liability_codes.shape[0],
        max_transfer_slots=transfer_cause_codes.shape[1],
        max_obligation_slots=obligation_cause_codes.shape[1],
        scheduled_sale_count=sale_month.shape[0],
        liquidity_policy_count=liquidity_policy_asset_codes.shape[0],
        max_liquidity_policy_assets=liquidity_policy_asset_codes.shape[1],
        max_tax_settlement_slots=max(1, len(scenario.tax_profiles)),
    )

    return CompiledSimulation(
        horizon_months=horizon,
        rollout_count=rollout_count,
        slot_plan=slot_plan,
        strings=tuple(strings.values),
        series_ids=series_ids,
        external_values=external_values,
        cash_agent_codes=np.asarray(cash_agent_codes, dtype=np.int64),
        cash_account_codes=np.asarray(cash_account_codes, dtype=np.int64),
        cash_initial_balance=np.asarray(cash_initial_balance, dtype=np.float64),
        lot_id_codes=np.asarray(lot_id_codes, dtype=np.int64),
        lot_agent_codes=np.asarray(lot_agent_codes, dtype=np.int64),
        lot_asset_codes=np.asarray(lot_asset_codes, dtype=np.int64),
        lot_purchase_month=np.asarray(lot_purchase_month, dtype=np.int64),
        lot_cost_basis_per_unit=np.asarray(lot_cost_basis_per_unit, dtype=np.float64),
        lot_initial_quantity=np.asarray(lot_initial_quantity, dtype=np.float64),
        tax_profile_agent_codes=tax_profile_agent_codes,
        tax_profile_payment_slot=tax_profile_payment_slot,
        tax_profile_payment_account_codes=tax_profile_payment_account_codes,
        tax_profile_authority_agent_codes=tax_profile_authority_agent_codes,
        tax_profile_authority_account_codes=tax_profile_authority_account_codes,
        tax_profile_prior_year_tax=tax_profile_prior_year_tax,
        capital_gain_agent_codes=capital_gain_agent_codes,
        tax_profile_capital_gain_index=tax_profile_capital_gain_index,
        tax_link_profile_index=tax_link_profile_index,
        tax_link_jurisdiction_codes=tax_link_jurisdiction_codes,
        tax_link_standard_deduction=tax_link_standard_deduction,
        tax_link_has_ltcg=tax_link_has_ltcg,
        tax_link_ordinary_upper=tax_link_ordinary_upper,
        tax_link_ordinary_rate=tax_link_ordinary_rate,
        tax_link_ordinary_count=tax_link_ordinary_count,
        tax_link_ltcg_upper=tax_link_ltcg_upper,
        tax_link_ltcg_rate=tax_link_ltcg_rate,
        tax_link_ltcg_count=tax_link_ltcg_count,
        tax_liability_profile_index=tax_liability_profile_index,
        tax_liability_link_index=tax_liability_link_index,
        tax_liability_year_end_month=tax_liability_year_end_month,
        transfer_cause_codes=transfer_cause_codes,
        transfer_from_agent_codes=transfer_from_agent_codes,
        transfer_from_account_codes=transfer_from_account_codes,
        transfer_from_cash_slot=transfer_from_cash_slot,
        transfer_to_agent_codes=transfer_to_agent_codes,
        transfer_to_account_codes=transfer_to_account_codes,
        transfer_to_cash_slot=transfer_to_cash_slot,
        transfer_income_profile_index=transfer_income_profile_index,
        transfer_amount_kind=transfer_amount_kind,
        transfer_amount_fixed=transfer_amount_fixed,
        transfer_amount_base=transfer_amount_base,
        transfer_amount_series_index=transfer_amount_series_index,
        transfer_amount_base_month=transfer_amount_base_month,
        transfer_amount_adjustment_period=transfer_amount_adjustment_period,
        property_cause_codes=property_cause_codes,
        property_id_codes=property_id_codes,
        property_location_codes=property_location_codes,
        property_location_tax_rate=property_location_tax_rate,
        property_month=property_month,
        property_buyer_agent_codes=property_buyer_agent_codes,
        property_buyer_account_codes=property_buyer_account_codes,
        property_buyer_cash_slot=property_buyer_cash_slot,
        property_seller_agent_codes=property_seller_agent_codes,
        property_seller_account_codes=property_seller_account_codes,
        property_seller_cash_slot=property_seller_cash_slot,
        property_purchase_price=property_purchase_price,
        property_closing_cost=property_closing_cost,
        property_down_payment=property_down_payment,
        property_adjusted_basis=property_adjusted_basis,
        property_ownership_pct=property_ownership_pct,
        property_stake_contribution=property_stake_contribution,
        property_equity_ledger=property_equity_ledger,
        property_mortgage_slot=property_mortgage_slot,
        liability_codes=liability_codes,
        liability_property_slot=liability_property_slot,
        liability_agent_codes=liability_agent_codes,
        liability_payment_account_codes=liability_payment_account_codes,
        liability_payment_cash_slot=liability_payment_cash_slot,
        liability_counterparty_agent_codes=liability_counterparty_agent_codes,
        liability_counterparty_account_codes=liability_counterparty_account_codes,
        liability_counterparty_cash_slot=liability_counterparty_cash_slot,
        liability_principal=liability_principal,
        liability_annual_rate=liability_annual_rate,
        liability_term_months=liability_term_months,
        liability_monthly_payment=liability_monthly_payment,
        sale_cause_codes=sale_cause_codes,
        sale_month=sale_month,
        sale_agent_codes=sale_agent_codes,
        sale_asset_codes=sale_asset_codes,
        sale_quantity=sale_quantity,
        sale_proceeds_account_codes=sale_proceeds_account_codes,
        sale_proceeds_cash_slot=sale_proceeds_cash_slot,
        sale_price_fixed=sale_price_fixed,
        sale_price_series_index=sale_price_series_index,
        obligation_cause_codes=obligation_cause_codes,
        obligation_id_codes=obligation_id_codes,
        obligation_type_codes=obligation_type_codes,
        obligation_agent_codes=obligation_agent_codes,
        obligation_from_account_codes=obligation_from_account_codes,
        obligation_from_cash_slot=obligation_from_cash_slot,
        obligation_to_agent_codes=obligation_to_agent_codes,
        obligation_to_account_codes=obligation_to_account_codes,
        obligation_to_cash_slot=obligation_to_cash_slot,
        obligation_amount_kind=obligation_amount_kind,
        obligation_amount_fixed=obligation_amount_fixed,
        obligation_amount_base=obligation_amount_base,
        obligation_amount_series_index=obligation_amount_series_index,
        obligation_amount_base_month=obligation_amount_base_month,
        obligation_amount_adjustment_period=obligation_amount_adjustment_period,
        obligation_source_kind=obligation_source_kind,
        obligation_source_index=obligation_source_index,
        tax_settlement_profile_index=tax_settlement_profile_index,
        liquidity_policy_agent_codes=liquidity_policy_agent_codes,
        liquidity_policy_account_codes=liquidity_policy_account_codes,
        liquidity_policy_cash_slot=liquidity_policy_cash_slot,
        liquidity_policy_buffer_trigger=liquidity_policy_buffer_trigger,
        liquidity_policy_buffer_sale=liquidity_policy_buffer_sale,
        liquidity_policy_asset_codes=liquidity_policy_asset_codes,
        liquidity_policy_asset_series_index=liquidity_policy_asset_series_index,
        liquidity_policy_prefixes=liquidity_policy_prefixes,
    )


def _collect_series_ids(scenario: Scenario, external_series: ExternalSeriesContext) -> tuple[str, ...]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(series_id: str) -> None:
        if series_id not in seen:
            seen.add(series_id)
            ids.append(series_id)

    for value in external_series.series_values.select("series_id").unique().get_column("series_id").to_list():
        add(str(value))
    for transfer in [*scenario.scheduled_transfers, *scenario.recurring_transfers]:
        _add_amount_series_id(transfer.amount_usd, add)
    for obligation in [*scenario.scheduled_obligations, *scenario.recurring_obligations]:
        _add_amount_series_id(obligation.amount_due_usd, add)
    for sale in scenario.scheduled_asset_sales:
        if sale.price_per_unit_usd is None:
            add(sale.asset_id)
    for policy in scenario.liquidity_policies:
        for asset_id in policy.asset_preference_chain:
            add(asset_id)
    return tuple(ids)


def _add_amount_series_id(amount: Any, add: Any) -> None:
    if isinstance(amount, SeriesIndexedAmount):
        add(amount.series_id)


def _external_values_cube(
    external_series: ExternalSeriesContext,
    *,
    series_index_by_id: dict[str, int],
    rollout_count: int,
    horizon_months: int,
) -> np.ndarray:
    values = np.full((len(series_index_by_id), rollout_count, horizon_months + 1), np.nan, dtype=np.float64)
    if external_series.series_values.is_empty():
        return values
    for row in external_series.series_values.iter_rows(named=True):
        series_index = series_index_by_id.get(str(row["series_id"]))
        if series_index is None:
            continue
        rollout_index = int(row["rollout_index"])
        month_index = int(row["month_index"])
        if 0 <= rollout_index < rollout_count and 0 <= month_index <= horizon_months:
            values[series_index, rollout_index, month_index] = float(row["value"])
    return values


def _slot(account_slot_by_key: dict[tuple[str, str], int], agent_id: str, account_id: str) -> int:
    return account_slot_by_key.get((agent_id, account_id), NO_CODE)


def _amount_arrays(amount: Any, series_index_by_id: dict[str, int]) -> tuple[int, float, float, int, int, int]:
    if isinstance(amount, int | float):
        return AMOUNT_FIXED, float(amount), 0.0, NO_CODE, 0, 1
    if isinstance(amount, FixedAmount):
        return AMOUNT_FIXED, float(amount.amount_usd), 0.0, NO_CODE, 0, 1
    if isinstance(amount, SeriesIndexedAmount):
        return (
            AMOUNT_SERIES_INDEXED,
            0.0,
            float(amount.base_amount_usd),
            series_index_by_id[amount.series_id],
            int(amount.base_month_index),
            int(amount.adjustment_period_months),
        )
    raise TypeError(f"unsupported amount spec: {amount!r}")


def _empty_month_matrix(months: int, slots: int, dtype: Any, fill: int | float = 0) -> np.ndarray:
    matrix = np.empty((months, max(1, slots)), dtype=dtype)
    matrix[...] = fill
    return matrix


def _compile_transfer_slots(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    profile_index_by_agent: dict[str, int],
    series_index_by_id: dict[str, int],
) -> tuple[np.ndarray, ...]:
    by_month: list[list[ScheduledTransfer | RecurringTransfer]] = []
    max_slots = 0
    horizon = int(scenario.horizon_months)
    for month in range(horizon):
        active: list[ScheduledTransfer | RecurringTransfer] = [
            t for t in scenario.scheduled_transfers if t.month == month
        ]
        active.extend(t for t in scenario.recurring_transfers if t.is_active_at(month))
        by_month.append(active)
        max_slots = max(max_slots, len(active))

    cause = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_agent = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_account = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_slot = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_agent = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_account = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_slot = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    income_profile = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_kind = _empty_month_matrix(horizon, max_slots, np.int64, AMOUNT_FIXED)
    amount_fixed = _empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_base = _empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_series = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_base_month = _empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_period = _empty_month_matrix(horizon, max_slots, np.int64, 1)

    for month, active in enumerate(by_month):
        for idx, transfer in enumerate(active):
            cause[month, idx] = strings.require(transfer.cause_id)
            from_agent[month, idx] = strings.require(transfer.from_agent_id)
            from_account[month, idx] = strings.require(transfer.from_account_id)
            from_slot[month, idx] = _slot(account_slot_by_key, transfer.from_agent_id, transfer.from_account_id)
            to_agent[month, idx] = strings.require(transfer.to_agent_id)
            to_account[month, idx] = strings.require(transfer.to_account_id)
            to_slot[month, idx] = _slot(account_slot_by_key, transfer.to_agent_id, transfer.to_account_id)
            if transfer.income_category == ORDINARY_INCOME_CATEGORY:
                income_profile[month, idx] = profile_index_by_agent.get(transfer.to_agent_id, NO_CODE)
            kind, fixed, base, series, base_month, period = _amount_arrays(transfer.amount_usd, series_index_by_id)
            amount_kind[month, idx] = kind
            amount_fixed[month, idx] = fixed
            amount_base[month, idx] = base
            amount_series[month, idx] = series
            amount_base_month[month, idx] = base_month
            amount_period[month, idx] = period
    return (
        cause,
        from_agent,
        from_account,
        from_slot,
        to_agent,
        to_account,
        to_slot,
        income_profile,
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
    )


def _compile_tax(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    jurisdictions: dict[str, Jurisdiction],
) -> tuple[np.ndarray, ...]:
    profile_agent = []
    payment_slot = []
    payment_account = []
    authority_agent = []
    authority_account = []
    prior_year_tax = []
    link_profile = []
    link_jurisdiction = []
    standard_deduction = []
    has_ltcg = []
    ordinary_brackets: list[list[tuple[float, float]]] = []
    ltcg_brackets: list[list[tuple[float, float]]] = []

    max_ord = 1
    max_ltcg = 1
    for profile_index, profile in enumerate(scenario.tax_profiles):
        profile_agent.append(strings.require(profile.agent_id))
        payment_slot.append(_slot(account_slot_by_key, profile.agent_id, profile.payment_account_id))
        payment_account.append(strings.require(profile.payment_account_id))
        authority_agent.append(strings.require(profile.tax_authority_agent_id))
        authority_account.append(strings.require(profile.tax_authority_account_id))
        prior_year_tax.append(float(profile.prior_year_tax_usd))
        for jurisdiction_id in profile.jurisdiction_ids:
            jurisdiction = jurisdictions[jurisdiction_id]
            ordinary = [
                (float(bracket.upper_usd), float(bracket.rate))
                for bracket in jurisdiction.ordinary_income_brackets[profile.filing_status]
            ]
            ltcg = (
                [
                    (float(bracket.upper_usd), float(bracket.rate))
                    for bracket in jurisdiction.ltcg_brackets[profile.filing_status]
                ]
                if jurisdiction.ltcg_brackets is not None
                else []
            )
            max_ord = max(max_ord, len(ordinary))
            max_ltcg = max(max_ltcg, len(ltcg))
            link_profile.append(profile_index)
            link_jurisdiction.append(strings.require(jurisdiction_id))
            standard_deduction.append(float(jurisdiction.standard_deduction[profile.filing_status]))
            has_ltcg.append(1 if jurisdiction.ltcg_brackets is not None else 0)
            ordinary_brackets.append(ordinary)
            ltcg_brackets.append(ltcg)

    link_count = len(link_profile)
    ordinary_upper = np.zeros((max(1, link_count), max_ord), dtype=np.float64)
    ordinary_rate = np.zeros((max(1, link_count), max_ord), dtype=np.float64)
    ordinary_count = np.zeros(max(1, link_count), dtype=np.int64)
    ltcg_upper = np.zeros((max(1, link_count), max_ltcg), dtype=np.float64)
    ltcg_rate = np.zeros((max(1, link_count), max_ltcg), dtype=np.float64)
    ltcg_count = np.zeros(max(1, link_count), dtype=np.int64)
    for idx, ordinary in enumerate(ordinary_brackets):
        ordinary_count[idx] = len(ordinary)
        for bracket_idx, (upper, rate) in enumerate(ordinary):
            ordinary_upper[idx, bracket_idx] = upper
            ordinary_rate[idx, bracket_idx] = rate
    for idx, ltcg in enumerate(ltcg_brackets):
        ltcg_count[idx] = len(ltcg)
        for bracket_idx, (upper, rate) in enumerate(ltcg):
            ltcg_upper[idx, bracket_idx] = upper
            ltcg_rate[idx, bracket_idx] = rate

    return (
        np.asarray(profile_agent, dtype=np.int64),
        np.asarray(payment_slot, dtype=np.int64),
        np.asarray(payment_account, dtype=np.int64),
        np.asarray(authority_agent, dtype=np.int64),
        np.asarray(authority_account, dtype=np.int64),
        np.asarray(prior_year_tax, dtype=np.float64),
        np.asarray(link_profile, dtype=np.int64),
        np.asarray(link_jurisdiction, dtype=np.int64),
        np.asarray(standard_deduction, dtype=np.float64),
        np.asarray(has_ltcg, dtype=np.int64),
        ordinary_upper,
        ordinary_rate,
        ordinary_count,
        ltcg_upper,
        ltcg_rate,
        ltcg_count,
    )


def _compile_capital_gain_agents(scenario: Scenario, strings: StringTable) -> tuple[np.ndarray, np.ndarray]:
    agent_ids: list[str] = []
    seen: set[str] = set()

    def add(agent_id: str) -> None:
        if agent_id in seen:
            return
        seen.add(agent_id)
        agent_ids.append(agent_id)

    for profile in scenario.tax_profiles:
        add(profile.agent_id)
    for lot in scenario.initial_lots:
        add(lot.agent_id)
    for sale in scenario.scheduled_asset_sales:
        add(sale.agent_id)
    for policy in scenario.liquidity_policies:
        add(policy.agent_id)

    index_by_agent = {agent_id: idx for idx, agent_id in enumerate(agent_ids)}
    return (
        np.asarray([strings.require(agent_id) for agent_id in agent_ids], dtype=np.int64),
        np.asarray([index_by_agent[profile.agent_id] for profile in scenario.tax_profiles], dtype=np.int64),
    )


def _compile_tax_liability_slots(horizon: int, tax_link_profile_index: np.ndarray) -> tuple[np.ndarray, ...]:
    profile_indices = []
    link_indices = []
    end_months = []
    for month in range(horizon):
        if month % 12 != 11:
            continue
        for link_index, profile_index in enumerate(tax_link_profile_index.tolist()):
            profile_indices.append(profile_index)
            link_indices.append(link_index)
            end_months.append(month)
    return (
        np.asarray(profile_indices, dtype=np.int64),
        np.asarray(link_indices, dtype=np.int64),
        np.asarray(end_months, dtype=np.int64),
    )


def _compile_properties_and_liabilities(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    locations: dict[str, Location],
) -> tuple[np.ndarray, ...]:
    prop_count = len(scenario.scheduled_property_purchases)
    cause = np.full((int(scenario.horizon_months), max(1, prop_count)), NO_CODE, dtype=np.int64)
    prop_id = np.zeros(max(1, prop_count), dtype=np.int64)
    location_id = np.zeros(max(1, prop_count), dtype=np.int64)
    location_tax_rate = np.zeros(max(1, prop_count), dtype=np.float64)
    month_array = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    buyer_agent = np.zeros(max(1, prop_count), dtype=np.int64)
    buyer_account = np.zeros(max(1, prop_count), dtype=np.int64)
    buyer_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    seller_agent = np.zeros(max(1, prop_count), dtype=np.int64)
    seller_account = np.zeros(max(1, prop_count), dtype=np.int64)
    seller_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    purchase_price = np.zeros(max(1, prop_count), dtype=np.float64)
    closing_cost = np.zeros(max(1, prop_count), dtype=np.float64)
    down_payment = np.zeros(max(1, prop_count), dtype=np.float64)
    adjusted_basis = np.zeros(max(1, prop_count), dtype=np.float64)
    ownership = np.zeros(max(1, prop_count), dtype=np.float64)
    stake_contribution = np.zeros(max(1, prop_count), dtype=np.float64)
    equity_ledger = np.zeros(max(1, prop_count), dtype=np.float64)
    mortgage_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)

    liability_codes = []
    liability_property_slot = []
    liability_agent = []
    liability_payment_account = []
    liability_payment_slot = []
    liability_counterparty_agent = []
    liability_counterparty_account = []
    liability_counterparty_slot = []
    liability_principal = []
    liability_rate = []
    liability_term = []
    liability_payment = []

    for idx, purchase in enumerate(scenario.scheduled_property_purchases):
        cause[purchase.month, idx] = strings.require(purchase.cause_id)
        prop_id[idx] = strings.require(purchase.property_id)
        location_id[idx] = strings.require(purchase.location_id)
        location = locations.get(purchase.location_id)
        location_tax_rate[idx] = 0.0 if location is None else float(location.annual_property_tax_rate)
        month_array[idx] = int(purchase.month)
        buyer_agent[idx] = strings.require(purchase.buyer_agent_id)
        buyer_account[idx] = strings.require(purchase.buyer_account_id)
        buyer_slot[idx] = _slot(account_slot_by_key, purchase.buyer_agent_id, purchase.buyer_account_id)
        seller_agent[idx] = strings.require(purchase.seller_agent_id)
        seller_account[idx] = strings.require(purchase.seller_account_id)
        seller_slot[idx] = _slot(account_slot_by_key, purchase.seller_agent_id, purchase.seller_account_id)
        mortgage_principal = purchase.mortgage.principal_usd if purchase.mortgage is not None else 0.0
        purchase_price[idx] = float(purchase.purchase_price_usd)
        closing_cost[idx] = float(purchase.buyer_closing_cost_usd)
        down_payment[idx] = float(purchase.down_payment_usd)
        adjusted_basis[idx] = float(purchase.purchase_price_usd + purchase.buyer_closing_cost_usd)
        ownership[idx] = float(purchase.ownership_pct)
        stake_contribution[idx] = float(purchase.down_payment_usd + purchase.buyer_closing_cost_usd)
        equity_ledger[idx] = float(purchase.purchase_price_usd - mortgage_principal)
        if purchase.mortgage is not None:
            slot = len(liability_codes)
            mortgage_slot[idx] = slot
            mortgage = purchase.mortgage
            liability_codes.append(strings.require(mortgage.liability_id))
            liability_property_slot.append(idx)
            liability_agent.append(strings.require(purchase.buyer_agent_id))
            liability_payment_account.append(strings.require(purchase.buyer_account_id))
            liability_payment_slot.append(
                _slot(account_slot_by_key, purchase.buyer_agent_id, purchase.buyer_account_id)
            )
            liability_counterparty_agent.append(strings.require(mortgage.lender_agent_id))
            liability_counterparty_account.append(strings.require(mortgage.lender_account_id))
            liability_counterparty_slot.append(
                _slot(account_slot_by_key, mortgage.lender_agent_id, mortgage.lender_account_id)
            )
            liability_principal.append(float(mortgage.principal_usd))
            liability_rate.append(float(mortgage.annual_interest_rate))
            liability_term.append(int(mortgage.term_months))
            liability_payment.append(
                mortgage_monthly_payment_usd(
                    mortgage.principal_usd, mortgage.annual_interest_rate, int(mortgage.term_months)
                )
            )

    return (
        cause,
        prop_id,
        location_id,
        location_tax_rate,
        month_array,
        buyer_agent,
        buyer_account,
        buyer_slot,
        seller_agent,
        seller_account,
        seller_slot,
        purchase_price,
        closing_cost,
        down_payment,
        adjusted_basis,
        ownership,
        stake_contribution,
        equity_ledger,
        mortgage_slot,
        np.asarray(liability_codes, dtype=np.int64),
        np.asarray(liability_property_slot, dtype=np.int64),
        np.asarray(liability_agent, dtype=np.int64),
        np.asarray(liability_payment_account, dtype=np.int64),
        np.asarray(liability_payment_slot, dtype=np.int64),
        np.asarray(liability_counterparty_agent, dtype=np.int64),
        np.asarray(liability_counterparty_account, dtype=np.int64),
        np.asarray(liability_counterparty_slot, dtype=np.int64),
        np.asarray(liability_principal, dtype=np.float64),
        np.asarray(liability_rate, dtype=np.float64),
        np.asarray(liability_term, dtype=np.int64),
        np.asarray(liability_payment, dtype=np.float64),
    )


def _compile_sales(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[str, int],
) -> tuple[np.ndarray, ...]:
    count = len(scenario.scheduled_asset_sales)
    cause = np.full((int(scenario.horizon_months), max(1, count)), NO_CODE, dtype=np.int64)
    month = np.full(max(1, count), NO_CODE, dtype=np.int64)
    agent = np.zeros(max(1, count), dtype=np.int64)
    asset = np.zeros(max(1, count), dtype=np.int64)
    quantity = np.zeros(max(1, count), dtype=np.float64)
    proceeds_account = np.zeros(max(1, count), dtype=np.int64)
    proceeds_slot = np.full(max(1, count), NO_CODE, dtype=np.int64)
    price_fixed = np.full(max(1, count), np.nan, dtype=np.float64)
    price_series = np.full(max(1, count), NO_CODE, dtype=np.int64)
    for idx, sale in enumerate(scenario.scheduled_asset_sales):
        cause[sale.month, idx] = strings.require(sale.cause_id)
        month[idx] = int(sale.month)
        agent[idx] = strings.require(sale.agent_id)
        asset[idx] = strings.require(sale.asset_id)
        quantity[idx] = float(sale.quantity)
        proceeds_account[idx] = strings.require(sale.proceeds_account_id)
        proceeds_slot[idx] = _slot(account_slot_by_key, sale.agent_id, sale.proceeds_account_id)
        if sale.price_per_unit_usd is not None:
            price_fixed[idx] = float(sale.price_per_unit_usd)
        else:
            price_series[idx] = series_index_by_id[sale.asset_id]
    return cause, month, agent, asset, quantity, proceeds_account, proceeds_slot, price_fixed, price_series


def _compile_obligation_slots(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[str, int],
    property_id_codes: np.ndarray,
    property_month: np.ndarray,
    liability_codes: np.ndarray,
    liability_property_slot: np.ndarray,
    tax_profile_prior_year_tax: np.ndarray,
) -> tuple[np.ndarray, ...]:
    horizon = int(scenario.horizon_months)
    monthly_specs: list[list[dict[str, Any]]] = [[] for _ in range(horizon)]

    for obligation in scenario.scheduled_obligations:
        if 0 <= obligation.month < horizon:
            monthly_specs[obligation.month].append({"kind": 0, "source": NO_CODE, "config": obligation})
    for month in range(horizon):
        for obligation in scenario.recurring_obligations:
            if obligation.is_active_at(month):
                monthly_specs[month].append({"kind": 0, "source": NO_CODE, "config": obligation})

    for month in range(horizon):
        for liability_slot, liability_code in enumerate(liability_codes.tolist()):
            prop_slot = int(liability_property_slot[liability_slot])
            monthly_specs[month].append(
                {"kind": 1, "source": liability_slot, "liability_code": liability_code, "prop_slot": prop_slot}
            )

    for month in range(horizon):
        for prop_slot, prop_code in enumerate(property_id_codes.tolist()):
            if prop_slot < property_month.shape[0]:
                monthly_specs[month].append({"kind": 2, "source": prop_slot, "property_code": prop_code})

    for month in range(horizon):
        quarter = _estimated_tax_quarter(month)
        if quarter in {1, 2, 3}:
            for profile_index, prior_year_tax in enumerate(tax_profile_prior_year_tax.tolist()):
                if prior_year_tax > 0:
                    monthly_specs[month].append({"kind": 3, "source": profile_index, "quarter": quarter})
        elif quarter == 4:
            tax_year = month // 12 - 1
            if tax_year >= 0:
                for profile_index in range(len(tax_profile_prior_year_tax)):
                    monthly_specs[month].append({"kind": 4, "source": profile_index, "tax_year": tax_year})
                    monthly_specs[month].append({"kind": 5, "source": profile_index, "tax_year": tax_year})

    max_slots = max(1, max((len(specs) for specs in monthly_specs), default=0))
    cause = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    obligation_id = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    obligation_type = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    agent = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_account = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    from_slot = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_agent = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_account = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    to_slot = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_kind = _empty_month_matrix(horizon, max_slots, np.int64, AMOUNT_FIXED)
    amount_fixed = _empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_base = _empty_month_matrix(horizon, max_slots, np.float64, 0.0)
    amount_series = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    amount_base_month = _empty_month_matrix(horizon, max_slots, np.int64, 0)
    amount_period = _empty_month_matrix(horizon, max_slots, np.int64, 1)
    source_kind = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    source_index = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)
    tax_settlement_profile = _empty_month_matrix(horizon, max_slots, np.int64, NO_CODE)

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
                from_slot[month, idx] = _slot(account_slot_by_key, config.agent_id, config.from_account_id)
                to_agent[month, idx] = strings.require(config.to_agent_id)
                to_account[month, idx] = strings.require(config.to_account_id)
                to_slot[month, idx] = _slot(account_slot_by_key, config.to_agent_id, config.to_account_id)
                kind, fixed, base, series, base_month, period = _amount_arrays(
                    config.amount_due_usd, series_index_by_id
                )
                amount_kind[month, idx] = kind
                amount_fixed[month, idx] = fixed
                amount_base[month, idx] = base
                amount_series[month, idx] = series
                amount_base_month[month, idx] = base_month
                amount_period[month, idx] = period
            elif spec["kind"] in {1, 2, 3, 4, 5}:
                # The dynamic source fields are decoded later from source_kind/source_index.
                continue

    # Fill dynamic source metadata after all strings that profiles/properties need are interned.
    for month, specs in enumerate(monthly_specs):
        for idx, spec in enumerate(specs):
            kind = int(spec["kind"])
            if kind == 1:
                liability_slot = int(spec["source"])
                if liability_slot >= liability_property_slot.shape[0]:
                    continue
                purchase = scenario.scheduled_property_purchases[int(liability_property_slot[liability_slot])]
                if purchase.mortgage is None:
                    continue
                cause_text = f"{purchase.mortgage.liability_id}_payment_m{month}"
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require("mortgage_payment")
                agent[month, idx] = strings.require(purchase.buyer_agent_id)
                from_account[month, idx] = strings.require(purchase.buyer_account_id)
                from_slot[month, idx] = _slot(account_slot_by_key, purchase.buyer_agent_id, purchase.buyer_account_id)
                to_agent[month, idx] = strings.require(purchase.mortgage.lender_agent_id)
                to_account[month, idx] = strings.require(purchase.mortgage.lender_account_id)
                to_slot[month, idx] = _slot(
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
                from_slot[month, idx] = _slot(account_slot_by_key, policy.owner_agent_id, policy.from_account_id)
                to_agent[month, idx] = strings.require(policy.tax_authority_agent_id)
                to_account[month, idx] = strings.require(policy.tax_authority_account_id)
                to_slot[month, idx] = _slot(
                    account_slot_by_key, policy.tax_authority_agent_id, policy.tax_authority_account_id
                )
                amount_fixed[month, idx] = (
                    float(policy.annual_tax_rate) if policy.annual_tax_rate is not None else np.nan
                )
            elif kind in {3, 4, 5}:
                profile_index = int(spec["source"])
                profile = profile_by_index[profile_index]
                if kind == 3:
                    quarter = int(spec["quarter"])
                    tax_year = month // 12
                    cause_text = f"{profile.agent_id}_estimated_tax_q{quarter}_y{tax_year}"
                    obligation_type_text = "estimated_tax"
                elif kind == 4:
                    tax_year = int(spec["tax_year"])
                    cause_text = f"{profile.agent_id}_estimated_tax_q4_y{tax_year}"
                    obligation_type_text = "estimated_tax"
                else:
                    tax_year = int(spec["tax_year"])
                    cause_text = f"{profile.agent_id}_tax_true_up_y{tax_year}"
                    obligation_type_text = "tax_true_up"
                    tax_settlement_profile[month, idx] = profile_index
                cause[month, idx] = strings.require(cause_text)
                obligation_id[month, idx] = strings.require(cause_text)
                obligation_type[month, idx] = strings.require(obligation_type_text)
                agent[month, idx] = strings.require(profile.agent_id)
                from_account[month, idx] = strings.require(profile.payment_account_id)
                from_slot[month, idx] = _slot(account_slot_by_key, profile.agent_id, profile.payment_account_id)
                to_agent[month, idx] = strings.require(profile.tax_authority_agent_id)
                to_account[month, idx] = strings.require(profile.tax_authority_account_id)
                to_slot[month, idx] = _slot(
                    account_slot_by_key, profile.tax_authority_agent_id, profile.tax_authority_account_id
                )
    return (
        cause,
        obligation_id,
        obligation_type,
        agent,
        from_account,
        from_slot,
        to_agent,
        to_account,
        to_slot,
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
        source_kind,
        source_index,
        tax_settlement_profile,
    )


def _estimated_tax_quarter(month: int) -> int | None:
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


def _compile_liquidity_policies(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[str, int],
) -> tuple[np.ndarray, ...]:
    policy_count = len(scenario.liquidity_policies)
    max_assets = max(1, max((len(policy.asset_preference_chain) for policy in scenario.liquidity_policies), default=0))
    agent = np.zeros(max(1, policy_count), dtype=np.int64)
    account = np.zeros(max(1, policy_count), dtype=np.int64)
    cash_slot = np.full(max(1, policy_count), NO_CODE, dtype=np.int64)
    trigger = np.zeros(max(1, policy_count), dtype=np.float64)
    sale = np.zeros(max(1, policy_count), dtype=np.float64)
    assets = np.full((max(1, policy_count), max_assets), NO_CODE, dtype=np.int64)
    asset_series = np.full((max(1, policy_count), max_assets), NO_CODE, dtype=np.int64)
    prefixes: list[str] = []
    for idx, policy in enumerate(scenario.liquidity_policies):
        agent[idx] = strings.require(policy.agent_id)
        account[idx] = strings.require(policy.account_id)
        cash_slot[idx] = _slot(account_slot_by_key, policy.agent_id, policy.account_id)
        trigger[idx] = float(policy.cash_buffer_trigger_below_usd)
        sale[idx] = float(policy.cash_buffer_sale_usd)
        prefixes.append(policy.cause_id_prefix)
        for asset_idx, asset_id in enumerate(policy.asset_preference_chain):
            assets[idx, asset_idx] = strings.require(asset_id)
            asset_series[idx, asset_idx] = series_index_by_id.get(asset_id, NO_CODE)
    return agent, account, cash_slot, trigger, sale, assets, asset_series, tuple(prefixes)
