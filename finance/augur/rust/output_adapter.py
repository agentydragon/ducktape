"""Decode Rust simulator trace JSON into Augur's canonical event frames.

The Rust engine keeps financial state in integer-native structs and emits one
trace per rollout.  This module is the compatibility boundary that lifts those
rows into the same ``EventLog`` schemas exposed by the Python/JAX backend.
State remains authoritative in the Rust snapshots; these frames are explanatory
output and are never replayed to reconstruct it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import polars as pl

from finance.augur.frames import FrameSpec
from finance.augur.sim.events import EVENT_FRAMES, EventLog

RATE_SCALE = 1_000_000_000


def decode_rust_event_log(output: Mapping[str, Any]) -> EventLog:
    """Return canonical funding/accounting event frames from Rust trace JSON."""

    transfers: list[dict[str, object]] = []
    dispositions: list[dict[str, object]] = []
    private_equity_events: list[dict[str, object]] = []
    private_equity_opportunities: list[dict[str, object]] = []
    obligation_accruals: list[dict[str, object]] = []
    obligation_settlements: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    property_purchases: list[dict[str, object]] = []
    mortgage_originations: list[dict[str, object]] = []
    mortgage_payments: list[dict[str, object]] = []
    primary_residence_events: list[dict[str, object]] = []
    rented_fraction_events: list[dict[str, object]] = []
    capital_improvements: list[dict[str, object]] = []
    property_sales: list[dict[str, object]] = []
    tax_accruals: list[dict[str, object]] = []
    tax_breakdowns: list[dict[str, object]] = []
    tax_settlements: list[dict[str, object]] = []

    for rollout in output["rollouts"]:
        rollout_index = int(rollout["rollout_id"])
        transfers.extend(_transfer_row(rollout_index, row) for row in rollout["transfers"])
        dispositions.extend(_disposition_row(rollout_index, row) for row in rollout["dispositions"])
        private_equity_events.extend(
            _private_equity_event_row(rollout_index, row) for row in rollout["private_equity_events"]
        )
        private_equity_opportunities.extend(
            _private_equity_opportunity_row(rollout_index, row) for row in rollout["private_equity_opportunities"]
        )
        for row in rollout["obligations"]:
            obligation_accruals.append(_obligation_accrual_row(rollout_index, row))
            obligation_settlements.append(_obligation_settlement_row(rollout_index, row))
        failures.extend(_failure_row(rollout_index, row) for row in rollout["rollout_failures"])
        property_purchases.extend(_property_purchase_row(rollout_index, row) for row in rollout["property_purchases"])
        mortgage_originations.extend(
            _mortgage_origination_row(rollout_index, row) for row in rollout["mortgage_originations"]
        )
        mortgage_payments.extend(_mortgage_payment_row(rollout_index, row) for row in rollout["mortgage_payments"])
        primary_residence_events.extend(
            _primary_residence_row(rollout_index, row) for row in rollout["primary_residence_events"]
        )
        rented_fraction_events.extend(
            _rented_fraction_row(rollout_index, row) for row in rollout["property_rented_fraction_events"]
        )
        capital_improvements.extend(
            _capital_improvement_row(rollout_index, row) for row in rollout["capital_improvements"]
        )
        property_sales.extend(_property_sale_row(rollout_index, row) for row in rollout["property_sales"])
        for row in rollout["tax_accruals"]:
            tax_accruals.append(_tax_accrual_row(rollout_index, row))
            tax_breakdowns.append(_tax_breakdown_row(rollout_index, row))
        tax_settlements.extend(_tax_settlement_row(rollout_index, row) for row in rollout["tax_settlements"])

    return EventLog.from_frames(
        {
            "transfers": _frame(EVENT_FRAMES.transfers, transfers),
            "lot_dispositions": _frame(EVENT_FRAMES.lot_dispositions, dispositions),
            "private_equity_events": _frame(EVENT_FRAMES.private_equity_events, private_equity_events),
            "private_equity_opportunities": _frame(
                EVENT_FRAMES.private_equity_opportunities, private_equity_opportunities
            ),
            "obligation_accruals": _frame(EVENT_FRAMES.obligation_accruals, obligation_accruals),
            "obligation_settlements": _frame(EVENT_FRAMES.obligation_settlements, obligation_settlements),
            "rollout_failures": _frame(EVENT_FRAMES.rollout_failures, failures),
            "property_purchases": _frame(EVENT_FRAMES.property_purchases, property_purchases),
            "mortgage_originations": _frame(EVENT_FRAMES.mortgage_originations, mortgage_originations),
            "mortgage_payments": _frame(EVENT_FRAMES.mortgage_payments, mortgage_payments),
            "set_primary_residence_events": _frame(EVENT_FRAMES.set_primary_residence_events, primary_residence_events),
            "set_rented_fraction_events": _frame(EVENT_FRAMES.set_rented_fraction_events, rented_fraction_events),
            "capital_improvement_events": _frame(EVENT_FRAMES.capital_improvement_events, capital_improvements),
            "property_sale_events": _frame(EVENT_FRAMES.property_sale_events, property_sales),
            "tax_accruals": _frame(EVENT_FRAMES.tax_accruals, tax_accruals),
            "tax_breakdowns": _frame(EVENT_FRAMES.tax_breakdowns, tax_breakdowns),
            "tax_settlements": _frame(EVENT_FRAMES.tax_settlements, tax_settlements),
        }
    )


def _frame(spec: FrameSpec, rows: list[dict[str, object]]) -> pl.DataFrame:
    return spec.empty() if not rows else pl.DataFrame(rows, schema=spec.schema)


def _transfer_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    source = row["from"]
    destination = row["to"]
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "from_agent_id": str(source["agent_id"]),
        "from_account_id": str(source["account_id"]),
        "to_agent_id": str(destination["agent_id"]),
        "to_account_id": str(destination["account_id"]),
        "amount_quanta": int(row["amount"]),
        "income_category": row["income_category"],
    }


def _disposition_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "agent_id": str(row["agent_id"]),
        "source_account_id": str(row["source_account_id"]),
        "asset_id": str(row["asset_id"]),
        "lot_id": str(row["lot_id"]),
        "purchase_month_index": int(row["purchase_month"]),
        "units_sold": int(row["units"]) / int(row["quantity_scale"]),
        "cost_basis_consumed_quanta": int(row["basis"]),
        "proceeds_quanta": int(row["proceeds"]),
        "proceeds_account_id": str(row["proceeds_account_id"]),
    }


def _private_equity_event_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "issuer_id": str(row["issuer_id"]),
        "asset_id": str(row["asset_id"]),
        "event_kind": str(row["event_kind"]),
        "regime": str(row["regime"]),
        "mark_quanta": int(row["mark"]),
        "sale_capacity_fraction": int(row["sale_capacity_fraction_ppb"]) / RATE_SCALE,
        "eligible_fraction": int(row["eligible_fraction_ppb"]) / RATE_SCALE,
        "forced_sale_fraction": int(row["forced_sale_fraction_ppb"]) / RATE_SCALE,
        "liquidity_blocked": bool(row["liquidity_blocked"]),
        "forced_recovery_cashout_quanta": int(row["forced_recovery_cashout"]),
    }


def _private_equity_opportunity_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    scale = int(row["quantity_scale"])
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "issuer_id": str(row["issuer_id"]),
        "asset_id": str(row["asset_id"]),
        "event_kind": str(row["event_kind"]),
        "regime": str(row["regime"]),
        "outcome": str(row["outcome"]),
        "mark_quanta": int(row["mark"]),
        "sale_capacity_fraction": int(row["sale_capacity_fraction_ppb"]) / RATE_SCALE,
        "eligible_fraction": int(row["eligible_fraction_ppb"]) / RATE_SCALE,
        "liquidity_blocked": bool(row["liquidity_blocked"]),
        "floor_quanta": int(row["floor"]),
        "liquid_net_worth_quanta": int(row["liquid_net_worth"]),
        "shortfall_quanta": int(row["shortfall"]),
        "units_held": int(row["units_held"]) / scale,
        "sellable_units": int(row["sellable_units"]) / scale,
        "target_units": int(row["target_units"]) / scale,
        "proceeds_quanta": int(row["proceeds"]),
    }


def _obligation_accrual_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    source = row["from"]
    destination = row["to"]
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "obligation_id": str(row["obligation_id"]),
        "obligation_type": str(row["obligation_type"]),
        "agent_id": str(source["agent_id"]),
        "from_account_id": str(source["account_id"]),
        "to_agent_id": str(destination["agent_id"]),
        "to_account_id": str(destination["account_id"]),
        "amount_due_quanta": int(row["amount_due"]),
    }


def _obligation_settlement_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    source = row["from"]
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "obligation_id": str(row["obligation_id"]),
        "obligation_type": str(row["obligation_type"]),
        "agent_id": str(source["agent_id"]),
        "from_account_id": str(source["account_id"]),
        "amount_due_quanta": int(row["amount_due"]),
        "amount_paid_quanta": int(row["amount_paid"]),
        "shortfall_quanta": int(row["shortfall"]),
        "attempted_funding_sources": str(row["attempted_funding_sources"]),
    }


def _failure_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "agent_id": str(row["agent_id"]),
        "deficit_quanta": int(row["deficit"]),
        "obligation_id": str(row["obligation_id"]),
        "obligation_type": str(row["obligation_type"]),
        "amount_due_quanta": int(row["amount_due"]),
        "amount_paid_quanta": int(row["amount_paid"]),
        "shortfall_quanta": int(row["shortfall"]),
        "attempted_funding_sources": str(row["attempted_funding_sources"]),
    }


def _property_purchase_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "property_id": str(row["property_id"]),
        "location_id": str(row["location_id"]),
        "buyer_agent_id": str(row["buyer_agent_id"]),
        "purchase_price_quanta": int(row["purchase_price"]),
        "closing_cost_quanta": int(row["closing_cost"]),
        "adjusted_basis_quanta": int(row["adjusted_basis"]),
        "stake_contribution_quanta": int(row["stake_contribution"]),
        "equity_ledger_quanta": int(row["equity_ledger"]),
    }


def _mortgage_origination_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "liability_id": str(row["liability_id"]),
        "agent_id": str(row["agent_id"]),
        "payment_account_id": str(row["payment_account_id"]),
        "counterparty_agent_id": str(row["counterparty_agent_id"]),
        "counterparty_account_id": str(row["counterparty_account_id"]),
        "property_id": str(row["property_id"]),
        "principal_quanta": int(row["principal"]),
        "annual_interest_rate": int(row["annual_interest_rate_ppb"]) / RATE_SCALE,
        "term_months": int(row["term_months"]),
        "monthly_payment_quanta": int(row["monthly_payment"]),
    }


def _mortgage_payment_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "liability_id": str(row["liability_id"]),
        "agent_id": str(row["agent_id"]),
        "counterparty_agent_id": str(row["counterparty_agent_id"]),
        "property_id": str(row["property_id"]),
        "from_account_id": str(row["from_account_id"]),
        "to_account_id": str(row["to_account_id"]),
        "interest_quanta": int(row["interest"]),
        "principal_quanta": int(row["principal"]),
        "total_payment_quanta": int(row["total_payment"]),
    }


def _rented_fraction_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "property_id": str(row["property_id"]),
        "rented_fraction": int(row["rented_fraction_ppb"]) / RATE_SCALE,
    }


def _primary_residence_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "agent_id": str(row["agent_id"]),
        "property_id": row["property_id"],
        "is_primary_residence": bool(row["is_primary_residence"]),
    }


def _capital_improvement_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "property_id": str(row["property_id"]),
        "amount_quanta": int(row["amount"]),
        "description": str(row["description"]),
    }


def _property_sale_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "property_id": str(row["property_id"]),
        "gross_proceeds_quanta": int(row["gross_proceeds"]),
        "mortgage_payoff_quanta": int(row["mortgage_payoff"]),
        "net_cash_to_owner_quanta": int(row["net_cash_to_owner"]),
        "realized_gain_quanta": int(row["realized_gain"]),
        "depreciation_recapture_quanta": int(row["depreciation_recapture"]),
        "section_121_exclusion_quanta": int(row["section_121_exclusion"]),
        "long_term_capital_gain_quanta": int(row["long_term_capital_gain"]),
    }


def _tax_accrual_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "agent_id": str(row["agent_id"]),
        "jurisdiction_id": str(row["jurisdiction_id"]),
        "tax_year_end_month": int(row["tax_year_end_month"]),
        "amount_quanta": int(row["total_tax"]),
    }


def _tax_breakdown_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "agent_id": str(row["agent_id"]),
        "jurisdiction_id": str(row["jurisdiction_id"]),
        "tax_year_end_month": int(row["tax_year_end_month"]),
        "ordinary_income_quanta": int(row["ordinary_income"]),
        "ltcg_quanta": int(row["long_term_gain"]),
        "stcg_quanta": int(row["short_term_gain"]),
        "standard_deduction_quanta": int(row["standard_deduction"]),
        "mortgage_interest_deduction_quanta": int(row["mortgage_interest_deduction"]),
        "salt_deduction_quanta": int(row["salt_deduction"]),
        "itemized_deduction_quanta": int(row["itemized_deduction"]),
        "ordinary_taxable_quanta": int(row["ordinary_taxable"]),
        "capital_gain_taxable_quanta": int(row["long_term_capital_gain_taxable"]),
        "ordinary_tax_quanta": int(row["ordinary_tax"]),
        "capital_gain_tax_quanta": int(row["capital_gain_tax"]),
        "total_tax_quanta": int(row["total_tax"]),
    }


def _tax_settlement_row(rollout_index: int, row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "rollout_index": rollout_index,
        "month_index": int(row["month"]),
        "cause_id": str(row["cause_id"]),
        "agent_id": str(row["agent_id"]),
        "tax_year_end_month": int(row["tax_year_end_month"]),
        "amount_quanta": int(row["amount"]),
    }
