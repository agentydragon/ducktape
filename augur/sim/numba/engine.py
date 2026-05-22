"""Numba-backed dense-array simulation engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from augur.sim.events import EVENT_FRAMES, EventLog
from augur.sim.external_series import ExternalSeriesContext
from augur.sim.numba.compiler import CompiledSimulation, compile_simulation
from augur.sim.numba.kernels import run_simulation_kernel
from augur.sim.run import SimulationRun
from augur.sim.runtime import load_jurisdictions_for, load_locations_for
from augur.sim.scenario import Scenario
from augur.sim.state import (
    ASSET_LOT_FRAME,
    CAPITAL_GAINS_YTD_FRAME,
    CASH_BALANCES_FRAME,
    LIABILITY_FRAME,
    ORDINARY_INCOME_YTD_FRAME,
    PROPERTY_STAKE_FRAME,
    PROPERTY_STATE_FRAME,
    ROLLOUT_STATUS_FRAME,
    TAX_LIABILITIES_FRAME,
)

NO_CODE = -1
LONG_TERM_CAPITAL_GAIN_CODE = 0
SHORT_TERM_CAPITAL_GAIN_CODE = 1
SOURCE_MORTGAGE_PAYMENT = 1
SOURCE_ESTIMATED_TAX = 3
SOURCE_ESTIMATED_TAX_Q4 = 4
SOURCE_TAX_TRUE_UP = 5


@dataclass
class _Buffers:
    cash_state: np.ndarray
    lot_state: np.ndarray
    ordinary_state: np.ndarray
    capital_gain_active_state: np.ndarray
    capital_gain_state: np.ndarray
    tax_liability_active_state: np.ndarray
    tax_liability_state: np.ndarray
    property_active_state: np.ndarray
    property_basis_state: np.ndarray
    property_ownership_state: np.ndarray
    property_contribution_state: np.ndarray
    property_equity_state: np.ndarray
    liability_active_state: np.ndarray
    liability_principal_state: np.ndarray
    liability_monthly_payment_state: np.ndarray
    liability_interest_ytd_state: np.ndarray
    liability_principal_ytd_state: np.ndarray
    rollout_failed_state: np.ndarray
    rollout_failed_month_state: np.ndarray
    transfer_active: np.ndarray
    transfer_amount: np.ndarray
    property_transfer_active: np.ndarray
    property_purchase_active: np.ndarray
    mortgage_origination_active: np.ndarray
    sched_disp_active: np.ndarray
    sched_disp_units: np.ndarray
    sched_disp_basis: np.ndarray
    sched_disp_proceeds: np.ndarray
    liq_disp_active: np.ndarray
    liq_disp_units: np.ndarray
    liq_disp_basis: np.ndarray
    liq_disp_proceeds: np.ndarray
    tax_accrual_active: np.ndarray
    tax_accrual_amount: np.ndarray
    tax_breakdown_ordinary: np.ndarray
    tax_breakdown_ltcg: np.ndarray
    tax_breakdown_stcg: np.ndarray
    tax_breakdown_ordinary_taxable: np.ndarray
    tax_breakdown_capital_taxable: np.ndarray
    tax_breakdown_ordinary_tax: np.ndarray
    tax_breakdown_capital_tax: np.ndarray
    obligation_active: np.ndarray
    obligation_due: np.ndarray
    obligation_paid: np.ndarray
    obligation_shortfall: np.ndarray
    obligation_attempt_policy: np.ndarray
    obligation_failure_active: np.ndarray
    mortgage_payment_active: np.ndarray
    mortgage_payment_interest: np.ndarray
    mortgage_payment_principal: np.ndarray
    mortgage_payment_total: np.ndarray
    tax_settlement_active: np.ndarray
    tax_settlement_amount: np.ndarray
    tax_settlement_year_end_month: np.ndarray


def simulate_with_external_series_numba(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext
) -> SimulationRun:
    plan = compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=load_locations_for(scenario),
    )
    buffers = _allocate_buffers(plan)
    run_simulation_kernel(
        plan.horizon_months,
        plan.rollout_count,
        plan.external_values,
        plan.cash_initial_balance,
        plan.lot_id_codes,
        plan.lot_agent_codes,
        plan.lot_asset_codes,
        plan.lot_purchase_month,
        plan.lot_cost_basis_per_unit,
        plan.lot_initial_quantity,
        plan.tax_profile_agent_codes,
        plan.tax_profile_prior_year_tax,
        plan.capital_gain_agent_codes,
        plan.tax_profile_capital_gain_index,
        plan.tax_link_profile_index,
        plan.tax_link_standard_deduction,
        plan.tax_link_has_ltcg,
        plan.tax_link_ordinary_upper,
        plan.tax_link_ordinary_rate,
        plan.tax_link_ordinary_count,
        plan.tax_link_ltcg_upper,
        plan.tax_link_ltcg_rate,
        plan.tax_link_ltcg_count,
        plan.tax_liability_profile_index,
        plan.tax_liability_link_index,
        plan.tax_liability_year_end_month,
        plan.transfer_cause_codes,
        plan.transfer_from_cash_slot,
        plan.transfer_to_cash_slot,
        plan.transfer_income_profile_index,
        plan.transfer_amount_kind,
        plan.transfer_amount_fixed,
        plan.transfer_amount_base,
        plan.transfer_amount_series_index,
        plan.transfer_amount_base_month,
        plan.transfer_amount_adjustment_period,
        plan.property_cause_codes,
        plan.property_month,
        plan.property_location_tax_rate,
        plan.property_buyer_cash_slot,
        plan.property_seller_cash_slot,
        plan.property_down_payment,
        plan.property_adjusted_basis,
        plan.property_ownership_pct,
        plan.property_stake_contribution,
        plan.property_equity_ledger,
        plan.property_mortgage_slot,
        plan.liability_property_slot,
        plan.liability_payment_cash_slot,
        plan.liability_counterparty_cash_slot,
        plan.liability_principal,
        plan.liability_annual_rate,
        plan.liability_term_months,
        plan.liability_monthly_payment,
        plan.sale_cause_codes,
        plan.sale_month,
        plan.sale_agent_codes,
        plan.sale_asset_codes,
        plan.sale_quantity,
        plan.sale_proceeds_cash_slot,
        plan.sale_price_fixed,
        plan.sale_price_series_index,
        plan.obligation_cause_codes,
        plan.obligation_agent_codes,
        plan.obligation_from_cash_slot,
        plan.obligation_to_cash_slot,
        plan.obligation_amount_kind,
        plan.obligation_amount_fixed,
        plan.obligation_amount_base,
        plan.obligation_amount_series_index,
        plan.obligation_amount_base_month,
        plan.obligation_amount_adjustment_period,
        plan.obligation_source_kind,
        plan.obligation_source_index,
        plan.tax_settlement_profile_index,
        plan.liquidity_policy_agent_codes,
        plan.liquidity_policy_cash_slot,
        plan.liquidity_policy_buffer_trigger,
        plan.liquidity_policy_buffer_sale,
        plan.liquidity_policy_asset_codes,
        plan.liquidity_policy_asset_series_index,
        buffers.cash_state,
        buffers.lot_state,
        buffers.ordinary_state,
        buffers.capital_gain_active_state,
        buffers.capital_gain_state,
        buffers.tax_liability_active_state,
        buffers.tax_liability_state,
        buffers.property_active_state,
        buffers.property_basis_state,
        buffers.property_ownership_state,
        buffers.property_contribution_state,
        buffers.property_equity_state,
        buffers.liability_active_state,
        buffers.liability_principal_state,
        buffers.liability_monthly_payment_state,
        buffers.liability_interest_ytd_state,
        buffers.liability_principal_ytd_state,
        buffers.rollout_failed_state,
        buffers.rollout_failed_month_state,
        buffers.transfer_active,
        buffers.transfer_amount,
        buffers.property_transfer_active,
        buffers.property_purchase_active,
        buffers.mortgage_origination_active,
        buffers.sched_disp_active,
        buffers.sched_disp_units,
        buffers.sched_disp_basis,
        buffers.sched_disp_proceeds,
        buffers.liq_disp_active,
        buffers.liq_disp_units,
        buffers.liq_disp_basis,
        buffers.liq_disp_proceeds,
        buffers.tax_accrual_active,
        buffers.tax_accrual_amount,
        buffers.tax_breakdown_ordinary,
        buffers.tax_breakdown_ltcg,
        buffers.tax_breakdown_stcg,
        buffers.tax_breakdown_ordinary_taxable,
        buffers.tax_breakdown_capital_taxable,
        buffers.tax_breakdown_ordinary_tax,
        buffers.tax_breakdown_capital_tax,
        buffers.obligation_active,
        buffers.obligation_due,
        buffers.obligation_paid,
        buffers.obligation_shortfall,
        buffers.obligation_attempt_policy,
        buffers.obligation_failure_active,
        buffers.mortgage_payment_active,
        buffers.mortgage_payment_interest,
        buffers.mortgage_payment_principal,
        buffers.mortgage_payment_total,
        buffers.tax_settlement_active,
        buffers.tax_settlement_amount,
        buffers.tax_settlement_year_end_month,
    )
    return _decode_run(plan, buffers, external_series)


def _allocate_buffers(plan: CompiledSimulation) -> _Buffers:
    h = plan.horizon_months
    r = plan.rollout_count
    cash_count = plan.cash_initial_balance.shape[0]
    lot_count = plan.lot_initial_quantity.shape[0]
    profile_count = plan.tax_profile_agent_codes.shape[0]
    capital_gain_agent_count = plan.capital_gain_agent_codes.shape[0]
    tax_liability_count = plan.tax_liability_profile_index.shape[0]
    property_count = plan.property_month.shape[0]
    liability_count = plan.liability_codes.shape[0]
    transfer_slots = plan.transfer_cause_codes.shape[1]
    obligation_slots = plan.obligation_cause_codes.shape[1]
    tax_links = max(1, plan.tax_link_profile_index.shape[0])
    return _Buffers(
        cash_state=np.zeros((h + 1, r, cash_count), dtype=np.float64),
        lot_state=np.zeros((h + 1, r, lot_count), dtype=np.float64),
        ordinary_state=np.zeros((h + 1, r, profile_count), dtype=np.float64),
        capital_gain_active_state=np.zeros((h + 1, r, capital_gain_agent_count, 2), dtype=np.bool_),
        capital_gain_state=np.zeros((h + 1, r, capital_gain_agent_count, 2), dtype=np.float64),
        tax_liability_active_state=np.zeros((h + 1, r, tax_liability_count), dtype=np.bool_),
        tax_liability_state=np.zeros((h + 1, r, tax_liability_count), dtype=np.float64),
        property_active_state=np.zeros((h + 1, r, property_count), dtype=np.bool_),
        property_basis_state=np.zeros((h + 1, r, property_count), dtype=np.float64),
        property_ownership_state=np.zeros((h + 1, r, property_count), dtype=np.float64),
        property_contribution_state=np.zeros((h + 1, r, property_count), dtype=np.float64),
        property_equity_state=np.zeros((h + 1, r, property_count), dtype=np.float64),
        liability_active_state=np.zeros((h + 1, r, liability_count), dtype=np.bool_),
        liability_principal_state=np.zeros((h + 1, r, liability_count), dtype=np.float64),
        liability_monthly_payment_state=np.zeros((h + 1, r, liability_count), dtype=np.float64),
        liability_interest_ytd_state=np.zeros((h + 1, r, liability_count), dtype=np.float64),
        liability_principal_ytd_state=np.zeros((h + 1, r, liability_count), dtype=np.float64),
        rollout_failed_state=np.zeros((h + 1, r), dtype=np.bool_),
        rollout_failed_month_state=np.full((h + 1, r), NO_CODE, dtype=np.int64),
        transfer_active=np.zeros((h, transfer_slots, r), dtype=np.bool_),
        transfer_amount=np.zeros((h, transfer_slots, r), dtype=np.float64),
        property_transfer_active=np.zeros((h, property_count, r), dtype=np.bool_),
        property_purchase_active=np.zeros((h, property_count, r), dtype=np.bool_),
        mortgage_origination_active=np.zeros((h, max(1, liability_count), r), dtype=np.bool_),
        sched_disp_active=np.zeros((h, plan.max_scheduled_disposition_slots, r), dtype=np.bool_),
        sched_disp_units=np.zeros((h, plan.max_scheduled_disposition_slots, r), dtype=np.float64),
        sched_disp_basis=np.zeros((h, plan.max_scheduled_disposition_slots, r), dtype=np.float64),
        sched_disp_proceeds=np.zeros((h, plan.max_scheduled_disposition_slots, r), dtype=np.float64),
        liq_disp_active=np.zeros((h, plan.max_liquidity_disposition_slots, r), dtype=np.bool_),
        liq_disp_units=np.zeros((h, plan.max_liquidity_disposition_slots, r), dtype=np.float64),
        liq_disp_basis=np.zeros((h, plan.max_liquidity_disposition_slots, r), dtype=np.float64),
        liq_disp_proceeds=np.zeros((h, plan.max_liquidity_disposition_slots, r), dtype=np.float64),
        tax_accrual_active=np.zeros((h, tax_links, r), dtype=np.bool_),
        tax_accrual_amount=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_ordinary=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_ltcg=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_stcg=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_ordinary_taxable=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_capital_taxable=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_ordinary_tax=np.zeros((h, tax_links, r), dtype=np.float64),
        tax_breakdown_capital_tax=np.zeros((h, tax_links, r), dtype=np.float64),
        obligation_active=np.zeros((h, obligation_slots, r), dtype=np.bool_),
        obligation_due=np.zeros((h, obligation_slots, r), dtype=np.float64),
        obligation_paid=np.zeros((h, obligation_slots, r), dtype=np.float64),
        obligation_shortfall=np.zeros((h, obligation_slots, r), dtype=np.float64),
        obligation_attempt_policy=np.full((h, obligation_slots, r), NO_CODE, dtype=np.int64),
        obligation_failure_active=np.zeros((h, obligation_slots, r), dtype=np.bool_),
        mortgage_payment_active=np.zeros((h, max(1, liability_count), r), dtype=np.bool_),
        mortgage_payment_interest=np.zeros((h, max(1, liability_count), r), dtype=np.float64),
        mortgage_payment_principal=np.zeros((h, max(1, liability_count), r), dtype=np.float64),
        mortgage_payment_total=np.zeros((h, max(1, liability_count), r), dtype=np.float64),
        tax_settlement_active=np.zeros((h, max(1, profile_count), r), dtype=np.bool_),
        tax_settlement_amount=np.zeros((h, max(1, profile_count), r), dtype=np.float64),
        tax_settlement_year_end_month=np.full((h, max(1, profile_count), r), NO_CODE, dtype=np.int64),
    )


def _decode_run(plan: CompiledSimulation, buffers: _Buffers, external_series: ExternalSeriesContext) -> SimulationRun:
    events = _decode_events(plan, buffers)
    return SimulationRun(
        cash_balances=_decode_cash(plan, buffers),
        asset_lots=_decode_asset_lots(plan, buffers),
        ordinary_income_ytd=_decode_ordinary_income(plan, buffers),
        capital_gains_ytd=_decode_capital_gains(plan, buffers),
        tax_liabilities=_decode_tax_liabilities(plan, buffers),
        property_state=_decode_property_state(plan, buffers),
        property_stakes=_decode_property_stakes(plan, buffers),
        liabilities=_decode_liabilities(plan, buffers),
        rollout_status_history=_decode_rollout_status_history(plan, buffers),
        rollout_status=_decode_final_rollout_status(plan, buffers),
        events_log=events,
        series_values=external_series.series_values,
    )


def _text(plan: CompiledSimulation, code: int) -> str | None:
    if code < 0:
        return None
    return plan.strings[code]


def _frame(rows: list[dict[str, Any]], spec: Any) -> pl.DataFrame:
    if not rows:
        return spec.empty()
    return spec.normalize(pl.DataFrame(rows))


def _state_history_frame(rows: list[dict[str, Any]], spec: Any) -> pl.DataFrame:
    columns = ["rollout_index", "month_index", *(name for name in spec.schema.names() if name != "rollout_index")]
    if rows:
        return pl.DataFrame(rows).select(columns)
    schema = pl.Schema(
        {
            "rollout_index": pl.Int64(),
            "month_index": pl.Int64(),
            **{name: dtype for name, dtype in spec.schema.items() if name != "rollout_index"},
        }
    )
    return schema.to_frame()


def _decode_cash(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows = [
        {
            "rollout_index": rollout,
            "month_index": month,
            "agent_id": _text(plan, plan.cash_agent_codes[slot]),
            "account_id": _text(plan, plan.cash_account_codes[slot]),
            "balance_usd": float(buffers.cash_state[month, rollout, slot]),
        }
        for month in range(plan.horizon_months + 1)
        for rollout in range(plan.rollout_count)
        for slot in range(plan.cash_initial_balance.shape[0])
    ]
    return _state_history_frame(rows, CASH_BALANCES_FRAME)


def _decode_asset_lots(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows = [
        {
            "rollout_index": rollout,
            "month_index": month,
            "lot_id": _text(plan, plan.lot_id_codes[slot]),
            "agent_id": _text(plan, plan.lot_agent_codes[slot]),
            "asset_id": _text(plan, plan.lot_asset_codes[slot]),
            "purchase_month_index": int(plan.lot_purchase_month[slot]),
            "cost_basis_per_unit_usd": float(plan.lot_cost_basis_per_unit[slot]),
            "remaining_quantity": float(buffers.lot_state[month, rollout, slot]),
        }
        for month in range(plan.horizon_months + 1)
        for rollout in range(plan.rollout_count)
        for slot in range(plan.lot_initial_quantity.shape[0])
    ]
    return _state_history_frame(rows, ASSET_LOT_FRAME)


def _decode_ordinary_income(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows = [
        {
            "rollout_index": rollout,
            "month_index": month,
            "agent_id": _text(plan, plan.tax_profile_agent_codes[profile]),
            "ordinary_income_usd": float(buffers.ordinary_state[month, rollout, profile]),
        }
        for month in range(plan.horizon_months + 1)
        for rollout in range(plan.rollout_count)
        for profile in range(plan.tax_profile_agent_codes.shape[0])
    ]
    return _state_history_frame(rows, ORDINARY_INCOME_YTD_FRAME)


def _decode_capital_gains(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for month in range(plan.horizon_months + 1):
        for rollout in range(plan.rollout_count):
            for profile in range(plan.capital_gain_agent_codes.shape[0]):
                for cls, classification in (
                    (LONG_TERM_CAPITAL_GAIN_CODE, "ltcg"),
                    (SHORT_TERM_CAPITAL_GAIN_CODE, "stcg"),
                ):
                    if not buffers.capital_gain_active_state[month, rollout, profile, cls]:
                        continue
                    rows.append(
                        {
                            "rollout_index": rollout,
                            "month_index": month,
                            "agent_id": _text(plan, plan.capital_gain_agent_codes[profile]),
                            "classification": classification,
                            "gain_usd": float(buffers.capital_gain_state[month, rollout, profile, cls]),
                        }
                    )
    return _state_history_frame(rows, CAPITAL_GAINS_YTD_FRAME)


def _decode_tax_liabilities(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for month in range(plan.horizon_months + 1):
        for rollout in range(plan.rollout_count):
            for slot in range(plan.tax_liability_profile_index.shape[0]):
                if not buffers.tax_liability_active_state[month, rollout, slot]:
                    continue
                link = int(plan.tax_liability_link_index[slot])
                profile = int(plan.tax_liability_profile_index[slot])
                rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "agent_id": _text(plan, plan.tax_profile_agent_codes[profile]),
                        "jurisdiction_id": _text(plan, plan.tax_link_jurisdiction_codes[link]),
                        "tax_year_end_month": int(plan.tax_liability_year_end_month[slot]),
                        "amount_owed_usd": float(buffers.tax_liability_state[month, rollout, slot]),
                    }
                )
    return _state_history_frame(rows, TAX_LIABILITIES_FRAME)


def _decode_property_state(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for month in range(plan.horizon_months + 1):
        for rollout in range(plan.rollout_count):
            for prop in range(plan.property_id_codes.shape[0]):
                if not buffers.property_active_state[month, rollout, prop]:
                    continue
                rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "property_id": _text(plan, plan.property_id_codes[prop]),
                        "location_id": _text(plan, plan.property_location_codes[prop]),
                        "purchase_month_index": int(plan.property_month[prop]),
                        "adjusted_basis_usd": float(buffers.property_basis_state[month, rollout, prop]),
                    }
                )
    return _state_history_frame(rows, PROPERTY_STATE_FRAME)


def _decode_property_stakes(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for month in range(plan.horizon_months + 1):
        for rollout in range(plan.rollout_count):
            for prop in range(plan.property_id_codes.shape[0]):
                if not buffers.property_active_state[month, rollout, prop]:
                    continue
                rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "property_id": _text(plan, plan.property_id_codes[prop]),
                        "agent_id": _text(plan, plan.property_buyer_agent_codes[prop]),
                        "ownership_pct": float(buffers.property_ownership_state[month, rollout, prop]),
                        "contribution_used_usd": float(buffers.property_contribution_state[month, rollout, prop]),
                        "equity_ledger_usd": float(buffers.property_equity_state[month, rollout, prop]),
                    }
                )
    return _state_history_frame(rows, PROPERTY_STAKE_FRAME)


def _decode_liabilities(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for month in range(plan.horizon_months + 1):
        for rollout in range(plan.rollout_count):
            for liab in range(plan.liability_codes.shape[0]):
                if not buffers.liability_active_state[month, rollout, liab]:
                    continue
                prop = int(plan.liability_property_slot[liab])
                rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "liability_id": _text(plan, plan.liability_codes[liab]),
                        "agent_id": _text(plan, plan.liability_agent_codes[liab]),
                        "payment_account_id": _text(plan, plan.liability_payment_account_codes[liab]),
                        "counterparty_agent_id": _text(plan, plan.liability_counterparty_agent_codes[liab]),
                        "counterparty_account_id": _text(plan, plan.liability_counterparty_account_codes[liab]),
                        "property_id": _text(plan, plan.property_id_codes[prop]),
                        "principal_usd": float(buffers.liability_principal_state[month, rollout, liab]),
                        "annual_interest_rate": float(plan.liability_annual_rate[liab]),
                        "term_months": int(plan.liability_term_months[liab]),
                        "origination_month_index": int(plan.property_month[prop]),
                        "monthly_payment_usd": float(buffers.liability_monthly_payment_state[month, rollout, liab]),
                        "interest_paid_ytd_usd": float(buffers.liability_interest_ytd_state[month, rollout, liab]),
                        "principal_paid_ytd_usd": float(buffers.liability_principal_ytd_state[month, rollout, liab]),
                    }
                )
    return _state_history_frame(rows, LIABILITY_FRAME)


def _decode_rollout_status_history(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    rows = [
        {
            "rollout_index": rollout,
            "month_index": month,
            "status": "failed_insufficient_cash" if buffers.rollout_failed_state[month, rollout] else "active",
            "failed_month": None
            if buffers.rollout_failed_month_state[month, rollout] < 0
            else int(buffers.rollout_failed_month_state[month, rollout]),
        }
        for month in range(plan.horizon_months + 1)
        for rollout in range(plan.rollout_count)
    ]
    return pl.DataFrame(
        rows,
        schema={
            "rollout_index": pl.Int64(),
            "month_index": pl.Int64(),
            "status": pl.Utf8(),
            "failed_month": pl.Int64(),
        },
    )


def _decode_final_rollout_status(plan: CompiledSimulation, buffers: _Buffers) -> pl.DataFrame:
    month = plan.horizon_months
    rows = [
        {
            "rollout_index": rollout,
            "status": "failed_insufficient_cash" if buffers.rollout_failed_state[month, rollout] else "active",
            "failed_month": None
            if buffers.rollout_failed_month_state[month, rollout] < 0
            else int(buffers.rollout_failed_month_state[month, rollout]),
        }
        for rollout in range(plan.rollout_count)
    ]
    return _frame(rows, ROLLOUT_STATUS_FRAME)


def _decode_events(plan: CompiledSimulation, buffers: _Buffers) -> EventLog:
    transfer_rows: list[dict[str, Any]] = []
    lot_rows: list[dict[str, Any]] = []
    tax_accrual_rows: list[dict[str, Any]] = []
    tax_breakdown_rows: list[dict[str, Any]] = []
    tax_settlement_rows: list[dict[str, Any]] = []
    obligation_rows: list[dict[str, Any]] = []
    obligation_settlement_rows: list[dict[str, Any]] = []
    property_purchase_rows: list[dict[str, Any]] = []
    mortgage_origination_rows: list[dict[str, Any]] = []
    mortgage_payment_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    lot_count = max(1, plan.lot_id_codes.shape[0])
    max_policy_assets = plan.liquidity_policy_asset_codes.shape[1]

    for month in range(plan.horizon_months):
        for slot in range(plan.transfer_cause_codes.shape[1]):
            for rollout in range(plan.rollout_count):
                if not buffers.transfer_active[month, slot, rollout]:
                    continue
                transfer_rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "cause_id": _text(plan, plan.transfer_cause_codes[month, slot]),
                        "from_agent_id": _text(plan, plan.transfer_from_agent_codes[month, slot]),
                        "from_account_id": _text(plan, plan.transfer_from_account_codes[month, slot]),
                        "to_agent_id": _text(plan, plan.transfer_to_agent_codes[month, slot]),
                        "to_account_id": _text(plan, plan.transfer_to_account_codes[month, slot]),
                        "amount_usd": float(buffers.transfer_amount[month, slot, rollout]),
                        "income_category": "ordinary" if plan.transfer_income_profile_index[month, slot] >= 0 else None,
                    }
                )
        for prop in range(plan.property_id_codes.shape[0]):
            for rollout in range(plan.rollout_count):
                if not buffers.property_purchase_active[month, prop, rollout]:
                    continue
                property_purchase_rows.append(_property_purchase_row(plan, prop, rollout, month))
                if buffers.property_transfer_active[month, prop, rollout]:
                    transfer_rows.append(_property_transfer_row(plan, prop, rollout, month))
        for slot in range(buffers.sched_disp_active.shape[1]):
            sale = slot // lot_count
            lot = slot % lot_count
            if sale >= plan.sale_month.shape[0] or lot >= plan.lot_id_codes.shape[0]:
                continue
            for rollout in range(plan.rollout_count):
                if not buffers.sched_disp_active[month, slot, rollout]:
                    continue
                lot_rows.append(
                    _lot_row(
                        plan,
                        rollout=rollout,
                        month=month,
                        cause_id=_text(plan, plan.sale_cause_codes[month, sale]),
                        agent_code=plan.sale_agent_codes[sale],
                        asset_code=plan.sale_asset_codes[sale],
                        lot=lot,
                        units=float(buffers.sched_disp_units[month, slot, rollout]),
                        basis=float(buffers.sched_disp_basis[month, slot, rollout]),
                        proceeds=float(buffers.sched_disp_proceeds[month, slot, rollout]),
                        proceeds_account_code=plan.sale_proceeds_account_codes[sale],
                    )
                )
        for slot in range(buffers.liq_disp_active.shape[1]):
            lot = slot % lot_count
            asset_flat = slot // lot_count
            policy = asset_flat // max_policy_assets
            asset_idx = asset_flat % max_policy_assets
            if (
                lot >= plan.lot_id_codes.shape[0]
                or policy >= plan.liquidity_policy_agent_codes.shape[0]
                or asset_idx >= plan.liquidity_policy_asset_codes.shape[1]
                or plan.liquidity_policy_asset_codes[policy, asset_idx] < 0
            ):
                continue
            for rollout in range(plan.rollout_count):
                if not buffers.liq_disp_active[month, slot, rollout]:
                    continue
                asset_code = plan.liquidity_policy_asset_codes[policy, asset_idx]
                cause_id = f"{plan.liquidity_policy_prefixes[policy]}_m{month}_{_text(plan, asset_code)}"
                lot_rows.append(
                    _lot_row(
                        plan,
                        rollout=rollout,
                        month=month,
                        cause_id=cause_id,
                        agent_code=plan.liquidity_policy_agent_codes[policy],
                        asset_code=asset_code,
                        lot=lot,
                        units=float(buffers.liq_disp_units[month, slot, rollout]),
                        basis=float(buffers.liq_disp_basis[month, slot, rollout]),
                        proceeds=float(buffers.liq_disp_proceeds[month, slot, rollout]),
                        proceeds_account_code=plan.liquidity_policy_account_codes[policy],
                    )
                )
        for link in range(plan.tax_link_profile_index.shape[0]):
            for rollout in range(plan.rollout_count):
                if not buffers.tax_accrual_active[month, link, rollout]:
                    continue
                profile = int(plan.tax_link_profile_index[link])
                cause_id = f"{_text(plan, plan.tax_profile_agent_codes[profile])}_{_text(plan, plan.tax_link_jurisdiction_codes[link])}_year_end_accrual_m{month}"
                tax = float(buffers.tax_accrual_amount[month, link, rollout])
                tax_accrual_rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "cause_id": cause_id,
                        "agent_id": _text(plan, plan.tax_profile_agent_codes[profile]),
                        "jurisdiction_id": _text(plan, plan.tax_link_jurisdiction_codes[link]),
                        "tax_year_end_month": month,
                        "amount_usd": tax,
                    }
                )
                tax_breakdown_rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "cause_id": cause_id,
                        "agent_id": _text(plan, plan.tax_profile_agent_codes[profile]),
                        "jurisdiction_id": _text(plan, plan.tax_link_jurisdiction_codes[link]),
                        "tax_year_end_month": month,
                        "ordinary_income_usd": float(buffers.tax_breakdown_ordinary[month, link, rollout]),
                        "ltcg_usd": float(buffers.tax_breakdown_ltcg[month, link, rollout]),
                        "stcg_usd": float(buffers.tax_breakdown_stcg[month, link, rollout]),
                        "standard_deduction_usd": float(plan.tax_link_standard_deduction[link]),
                        "ordinary_taxable_usd": float(buffers.tax_breakdown_ordinary_taxable[month, link, rollout]),
                        "capital_gain_taxable_usd": float(buffers.tax_breakdown_capital_taxable[month, link, rollout]),
                        "ordinary_tax_usd": float(buffers.tax_breakdown_ordinary_tax[month, link, rollout]),
                        "capital_gain_tax_usd": float(buffers.tax_breakdown_capital_tax[month, link, rollout]),
                        "total_tax_usd": tax,
                    }
                )
        for slot in range(plan.obligation_cause_codes.shape[1]):
            for rollout in range(plan.rollout_count):
                if not buffers.obligation_active[month, slot, rollout]:
                    continue
                obligation_rows.append(_obligation_row(plan, buffers, month, slot, rollout))
                obligation_settlement_rows.append(_obligation_settlement_row(plan, buffers, month, slot, rollout))
                if buffers.obligation_paid[month, slot, rollout] > 0:
                    transfer_rows.append(_obligation_transfer_row(plan, buffers, month, slot, rollout))
                if buffers.obligation_failure_active[month, slot, rollout]:
                    failure_rows.append(_failure_row(plan, buffers, month, slot, rollout))
        for liab in range(plan.liability_codes.shape[0]):
            for rollout in range(plan.rollout_count):
                if buffers.mortgage_origination_active[month, liab, rollout]:
                    mortgage_origination_rows.append(_mortgage_origination_row(plan, liab, rollout, month))
                if buffers.mortgage_payment_active[month, liab, rollout]:
                    mortgage_payment_rows.append(_mortgage_payment_row(plan, buffers, liab, rollout, month))
        for profile in range(plan.tax_profile_agent_codes.shape[0]):
            for rollout in range(plan.rollout_count):
                if not buffers.tax_settlement_active[month, profile, rollout]:
                    continue
                year_end = int(buffers.tax_settlement_year_end_month[month, profile, rollout])
                tax_year = (year_end - 11) // 12
                tax_settlement_rows.append(
                    {
                        "rollout_index": rollout,
                        "month_index": month,
                        "cause_id": f"{_text(plan, plan.tax_profile_agent_codes[profile])}_tax_settlement_y{tax_year}",
                        "agent_id": _text(plan, plan.tax_profile_agent_codes[profile]),
                        "tax_year_end_month": year_end,
                        "amount_usd": float(buffers.tax_settlement_amount[month, profile, rollout]),
                    }
                )

    return EventLog.from_frames(
        {
            "transfers": _frame(transfer_rows, EVENT_FRAMES.transfers),
            "lot_dispositions": _frame(lot_rows, EVENT_FRAMES.lot_dispositions),
            "tax_accruals": _frame(tax_accrual_rows, EVENT_FRAMES.tax_accruals),
            "tax_breakdowns": _frame(tax_breakdown_rows, EVENT_FRAMES.tax_breakdowns),
            "tax_settlements": _frame(tax_settlement_rows, EVENT_FRAMES.tax_settlements),
            "obligation_accruals": _frame(obligation_rows, EVENT_FRAMES.obligation_accruals),
            "obligation_settlements": _frame(obligation_settlement_rows, EVENT_FRAMES.obligation_settlements),
            "property_purchases": _frame(property_purchase_rows, EVENT_FRAMES.property_purchases),
            "mortgage_originations": _frame(mortgage_origination_rows, EVENT_FRAMES.mortgage_originations),
            "mortgage_payments": _frame(mortgage_payment_rows, EVENT_FRAMES.mortgage_payments),
            "rollout_failures": _frame(failure_rows, EVENT_FRAMES.rollout_failures),
        }
    )


def _property_purchase_row(plan: CompiledSimulation, prop: int, rollout: int, month: int) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": _text(plan, plan.property_cause_codes[month, prop]),
        "property_id": _text(plan, plan.property_id_codes[prop]),
        "location_id": _text(plan, plan.property_location_codes[prop]),
        "buyer_agent_id": _text(plan, plan.property_buyer_agent_codes[prop]),
        "purchase_price_usd": float(plan.property_purchase_price[prop]),
        "closing_cost_usd": float(plan.property_closing_cost[prop]),
        "adjusted_basis_usd": float(plan.property_adjusted_basis[prop]),
        "ownership_pct": float(plan.property_ownership_pct[prop]),
        "stake_contribution_usd": float(plan.property_stake_contribution[prop]),
        "equity_ledger_usd": float(plan.property_equity_ledger[prop]),
    }


def _property_transfer_row(plan: CompiledSimulation, prop: int, rollout: int, month: int) -> dict[str, Any]:
    cause = _text(plan, plan.property_cause_codes[month, prop])
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": f"{cause}_buyer_cash",
        "from_agent_id": _text(plan, plan.property_buyer_agent_codes[prop]),
        "from_account_id": _text(plan, plan.property_buyer_account_codes[prop]),
        "to_agent_id": _text(plan, plan.property_seller_agent_codes[prop]),
        "to_account_id": _text(plan, plan.property_seller_account_codes[prop]),
        "amount_usd": float(plan.property_stake_contribution[prop]),
        "income_category": None,
    }


def _lot_row(
    plan: CompiledSimulation,
    *,
    rollout: int,
    month: int,
    cause_id: str | None,
    agent_code: int,
    asset_code: int,
    lot: int,
    units: float,
    basis: float,
    proceeds: float,
    proceeds_account_code: int,
) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": cause_id,
        "agent_id": _text(plan, agent_code),
        "asset_id": _text(plan, asset_code),
        "lot_id": _text(plan, plan.lot_id_codes[lot]),
        "purchase_month_index": int(plan.lot_purchase_month[lot]),
        "units_sold": units,
        "cost_basis_consumed_usd": basis,
        "proceeds_usd": proceeds,
        "proceeds_account_id": _text(plan, proceeds_account_code),
    }


def _obligation_row(plan: CompiledSimulation, buffers: _Buffers, month: int, slot: int, rollout: int) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": _text(plan, plan.obligation_cause_codes[month, slot]),
        "obligation_id": _text(plan, plan.obligation_id_codes[month, slot]),
        "obligation_type": _text(plan, plan.obligation_type_codes[month, slot]),
        "agent_id": _text(plan, plan.obligation_agent_codes[month, slot]),
        "from_account_id": _text(plan, plan.obligation_from_account_codes[month, slot]),
        "to_agent_id": _text(plan, plan.obligation_to_agent_codes[month, slot]),
        "to_account_id": _text(plan, plan.obligation_to_account_codes[month, slot]),
        "amount_due_usd": float(buffers.obligation_due[month, slot, rollout]),
    }


def _attempted_sources(plan: CompiledSimulation, policy: int) -> str:
    if policy < 0:
        return ""
    return ",".join(
        _text(plan, asset_code) or ""
        for asset_code in plan.liquidity_policy_asset_codes[policy].tolist()
        if asset_code >= 0
    )


def _obligation_settlement_row(
    plan: CompiledSimulation, buffers: _Buffers, month: int, slot: int, rollout: int
) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": _text(plan, plan.obligation_cause_codes[month, slot]),
        "obligation_id": _text(plan, plan.obligation_id_codes[month, slot]),
        "obligation_type": _text(plan, plan.obligation_type_codes[month, slot]),
        "agent_id": _text(plan, plan.obligation_agent_codes[month, slot]),
        "from_account_id": _text(plan, plan.obligation_from_account_codes[month, slot]),
        "amount_due_usd": float(buffers.obligation_due[month, slot, rollout]),
        "amount_paid_usd": float(buffers.obligation_paid[month, slot, rollout]),
        "shortfall_usd": float(buffers.obligation_shortfall[month, slot, rollout]),
        "attempted_funding_sources": _attempted_sources(
            plan, int(buffers.obligation_attempt_policy[month, slot, rollout])
        ),
    }


def _obligation_transfer_row(
    plan: CompiledSimulation, buffers: _Buffers, month: int, slot: int, rollout: int
) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": _text(plan, plan.obligation_cause_codes[month, slot]),
        "from_agent_id": _text(plan, plan.obligation_agent_codes[month, slot]),
        "from_account_id": _text(plan, plan.obligation_from_account_codes[month, slot]),
        "to_agent_id": _text(plan, plan.obligation_to_agent_codes[month, slot]),
        "to_account_id": _text(plan, plan.obligation_to_account_codes[month, slot]),
        "amount_usd": float(buffers.obligation_paid[month, slot, rollout]),
        "income_category": None,
    }


def _failure_row(plan: CompiledSimulation, buffers: _Buffers, month: int, slot: int, rollout: int) -> dict[str, Any]:
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": f"{_text(plan, plan.obligation_id_codes[month, slot])}_failure",
        "agent_id": _text(plan, plan.obligation_agent_codes[month, slot]),
        "deficit_usd": float(buffers.obligation_shortfall[month, slot, rollout]),
        "obligation_id": _text(plan, plan.obligation_id_codes[month, slot]),
        "obligation_type": _text(plan, plan.obligation_type_codes[month, slot]),
        "amount_due_usd": float(buffers.obligation_due[month, slot, rollout]),
        "amount_paid_usd": float(buffers.obligation_paid[month, slot, rollout]),
        "shortfall_usd": float(buffers.obligation_shortfall[month, slot, rollout]),
        "attempted_funding_sources": _attempted_sources(
            plan, int(buffers.obligation_attempt_policy[month, slot, rollout])
        ),
    }


def _mortgage_origination_row(plan: CompiledSimulation, liab: int, rollout: int, month: int) -> dict[str, Any]:
    prop = int(plan.liability_property_slot[liab])
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": f"{_text(plan, plan.property_cause_codes[month, prop])}_mortgage_origination",
        "liability_id": _text(plan, plan.liability_codes[liab]),
        "agent_id": _text(plan, plan.liability_agent_codes[liab]),
        "payment_account_id": _text(plan, plan.liability_payment_account_codes[liab]),
        "counterparty_agent_id": _text(plan, plan.liability_counterparty_agent_codes[liab]),
        "counterparty_account_id": _text(plan, plan.liability_counterparty_account_codes[liab]),
        "property_id": _text(plan, plan.property_id_codes[prop]),
        "principal_usd": float(plan.liability_principal[liab]),
        "annual_interest_rate": float(plan.liability_annual_rate[liab]),
        "term_months": int(plan.liability_term_months[liab]),
        "monthly_payment_usd": float(plan.liability_monthly_payment[liab]),
    }


def _mortgage_payment_row(
    plan: CompiledSimulation, buffers: _Buffers, liab: int, rollout: int, month: int
) -> dict[str, Any]:
    prop = int(plan.liability_property_slot[liab])
    return {
        "rollout_index": rollout,
        "month_index": month,
        "cause_id": f"{_text(plan, plan.liability_codes[liab])}_payment_m{month}",
        "liability_id": _text(plan, plan.liability_codes[liab]),
        "agent_id": _text(plan, plan.liability_agent_codes[liab]),
        "counterparty_agent_id": _text(plan, plan.liability_counterparty_agent_codes[liab]),
        "property_id": _text(plan, plan.property_id_codes[prop]),
        "from_account_id": _text(plan, plan.liability_payment_account_codes[liab]),
        "to_account_id": _text(plan, plan.liability_counterparty_account_codes[liab]),
        "interest_usd": float(buffers.mortgage_payment_interest[month, liab, rollout]),
        "principal_usd": float(buffers.mortgage_payment_principal[month, liab, rollout]),
        "total_payment_usd": float(buffers.mortgage_payment_total[month, liab, rollout]),
    }
