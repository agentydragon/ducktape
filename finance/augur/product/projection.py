"""Product rollout read model, projected from the canonical event frames.

One selected trajectory, rendered from the frames every engine emits plus that engine's
product metric arrays. Nothing here names an engine's own output layout, which is what
lets the rollout endpoint be served by either simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import polars as pl

from finance.augur.product.asset_key import AssetKey, parse_asset_key
from finance.augur.product.wire import (
    ROLLOUT_EVENT_KIND_ORDER,
    CapitalImprovementMarkerEvent,
    ClosingCostPaymentEvent,
    HoaDuesPaymentEvent,
    HoldingSaleEvent,
    HomeownersInsurancePaymentEvent,
    MonthlyExpenseEvent,
    MortgagePaymentEvent,
    OutsideRentPaymentEvent,
    PrivateEquityMarkerEvent,
    PrivateEquityOpportunityEvent,
    PropertyMaintenancePaymentEvent,
    PropertyPurchaseEvent,
    PropertySaleMarkerEvent,
    PropertyTaxPaymentEvent,
    RolloutEvent,
    RolloutFailureEvent,
    SetPrimaryResidenceMarkerEvent,
    SetRentedFractionMarkerEvent,
    TaxAccrualEvent,
    TaxPaymentEvent,
)
from finance.augur.sim.events import EventLog
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.scenario import ObligationType

_TAX_PAYMENT_OBLIGATION_TYPES = frozenset((ObligationType.ESTIMATED_TAX, ObligationType.TAX_TRUE_UP))
_EVENT_PRIORITY = {kind: priority for priority, kind in enumerate(ROLLOUT_EVENT_KIND_ORDER)}


@dataclass(frozen=True)
class ProductRolloutProjection:
    """One selected rollout in the product API's native read model."""

    currency_code: str
    currency_quantum: str
    monthly_metric_arrays: dict[str, np.ndarray]
    failed_month_index: int | None
    events: tuple[RolloutEvent, ...]


def _quanta(value: int | np.integer[Any]) -> str:
    return str(int(value))


def project_product_rollout(
    events: EventLog,
    metrics: ProductMetricArrays,
    *,
    rollout_index: int,
    primary_agent_id: str,
    asset_label_by_id: dict[str, str],
) -> ProductRolloutProjection:
    """One rollout, projected from the canonical event frames rather than an engine's arrays.

    Reads the canonical event frames rather than any engine's own output layout, so either
    engine can answer it and a Rust trace equals a JAX trace because one projection read
    both — not because two projections agreed.

    The frames already did most of the joining this file used to do by hand: seven payment
    shapes are `obligation_settlements` filtered by `obligation_type`, the three disposition
    sources are one `lot_dispositions`, and `tax_breakdowns` carries every column the accrual
    event needs instead of a `TaxBreakdownChannel` index per field.
    """

    if rollout_index < 0:
        raise IndexError(f"rollout_index {rollout_index} is negative")

    monthly_metric_arrays = {
        name: values.copy() if name == "month_index" else values[:, rollout_index].copy()
        for name, values in metrics.metric_arrays().items()
    }
    failed_month = int(metrics.failed_month[rollout_index])

    def rows(frame: pl.DataFrame, **equals: str) -> list[dict[str, Any]]:
        selected = frame.filter(pl.col("rollout_index") == rollout_index)
        for column, value in equals.items():
            selected = selected.filter(pl.col(column) == value)
        return selected.to_dicts()

    def settlements(*obligation_types: ObligationType) -> list[dict[str, Any]]:
        wanted = {obligation_type.value for obligation_type in obligation_types}
        return sorted(
            (
                row
                for row in rows(events.obligation_settlements, agent_id=primary_agent_id)
                if row["obligation_type"] in wanted
            ),
            key=lambda row: (row["month_index"], row["obligation_id"]),
        )

    def payments(event_type: type[Any], *obligation_types: ObligationType) -> list[RolloutEvent]:
        return [
            event_type(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["amount_paid_quanta"]),
                amount_due_quanta=_quanta(row["amount_due_quanta"]),
                amount_paid_quanta=_quanta(row["amount_paid_quanta"]),
                shortfall_quanta=_quanta(row["shortfall_quanta"]),
            )
            for row in settlements(*obligation_types)
        ]

    def asset_of(row: dict[str, Any]) -> AssetKey:
        return parse_asset_key(row["asset_id"])

    holding_sales: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows(events.lot_dispositions, agent_id=primary_agent_id):
        key = (row["month_index"], row["asset_id"])
        total = holding_sales.setdefault(key, {"units": 0.0, "proceeds": 0, "basis": 0})
        total["units"] += row["units_sold"]
        total["proceeds"] += row["proceeds_quanta"]
        total["basis"] += row["cost_basis_consumed_quanta"]

    mortgage_principal_by_property = {
        (row["month_index"], row["property_id"]): row["principal_quanta"] for row in rows(events.mortgage_originations)
    }
    accrual_amount = {
        (row["month_index"], row["jurisdiction_id"]): row["amount_quanta"]
        for row in rows(events.tax_accruals, agent_id=primary_agent_id)
    }

    projected: list[RolloutEvent] = [
        *(
            HoldingSaleEvent(
                month_index=month,
                amount_quanta=_quanta(total["proceeds"]),
                asset=parse_asset_key(asset_id),
                asset_label=asset_label_by_id.get(asset_id),
                units=total["units"],
                proceeds_quanta=_quanta(total["proceeds"]),
                cost_basis_quanta=_quanta(total["basis"]),
            )
            for (month, asset_id), total in sorted(holding_sales.items())
        ),
        *(
            PropertyPurchaseEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["purchase_price_quanta"]),
                property_id=row["property_id"],
                purchase_price_quanta=_quanta(row["purchase_price_quanta"]),
                down_payment_quanta=_quanta(row["equity_ledger_quanta"]),
                mortgage_principal_quanta=_quanta(
                    mortgage_principal_by_property.get((row["month_index"], row["property_id"]), 0)
                ),
            )
            for row in rows(events.property_purchases, buyer_agent_id=primary_agent_id)
        ),
        *(
            ClosingCostPaymentEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["closing_cost_quanta"]),
                property_id=row["property_id"],
            )
            for row in rows(events.property_purchases, buyer_agent_id=primary_agent_id)
            if row["closing_cost_quanta"]
        ),
        *(
            PrivateEquityMarkerEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["mark_quanta"]),
                issuer_id=row["issuer_id"],
                asset=asset_of(row),
                asset_label=asset_label_by_id.get(row["asset_id"]),
                event_kind=row["event_kind"],
                regime=row["regime"],
                mark_quanta=_quanta(row["mark_quanta"]),
                sale_capacity_fraction=row["sale_capacity_fraction"],
                eligible_fraction=row["eligible_fraction"],
                forced_sale_fraction=row["forced_sale_fraction"],
                liquidity_blocked=row["liquidity_blocked"],
                forced_recovery_cashout_quanta=_quanta(row["forced_recovery_cashout_quanta"]),
            )
            for row in rows(events.private_equity_events)
        ),
        *(
            PrivateEquityOpportunityEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["proceeds_quanta"]),
                issuer_id=row["issuer_id"],
                asset=asset_of(row),
                asset_label=asset_label_by_id.get(row["asset_id"]),
                event_kind=row["event_kind"],
                regime=row["regime"],
                outcome=row["outcome"],
                mark_quanta=_quanta(row["mark_quanta"]),
                sale_capacity_fraction=row["sale_capacity_fraction"],
                eligible_fraction=row["eligible_fraction"],
                liquidity_blocked=row["liquidity_blocked"],
                floor_quanta=_quanta(row["floor_quanta"]),
                liquid_net_worth_quanta=_quanta(row["liquid_net_worth_quanta"]),
                shortfall_quanta=_quanta(row["shortfall_quanta"]),
                units_held=row["units_held"],
                sellable_units=row["sellable_units"],
                target_units=row["target_units"],
                proceeds_quanta=_quanta(row["proceeds_quanta"]),
            )
            for row in rows(events.private_equity_opportunities)
        ),
        *(
            MortgagePaymentEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["total_payment_quanta"]),
                interest_quanta=_quanta(row["interest_quanta"]),
                principal_quanta=_quanta(row["principal_quanta"]),
            )
            for row in rows(events.mortgage_payments, agent_id=primary_agent_id)
        ),
        *payments(PropertyTaxPaymentEvent, ObligationType.PROPERTY_TAX),
        *payments(HoaDuesPaymentEvent, ObligationType.HOA_DUES),
        *payments(HomeownersInsurancePaymentEvent, ObligationType.HOMEOWNERS_INSURANCE),
        *payments(PropertyMaintenancePaymentEvent, ObligationType.PROPERTY_MAINTENANCE),
        *payments(MonthlyExpenseEvent, ObligationType.CASH_SPEND),
        *payments(OutsideRentPaymentEvent, ObligationType.OUTSIDE_RENT),
        *(
            TaxPaymentEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["amount_paid_quanta"]),
                obligation_type=row["obligation_type"],
                amount_due_quanta=_quanta(row["amount_due_quanta"]),
                amount_paid_quanta=_quanta(row["amount_paid_quanta"]),
                shortfall_quanta=_quanta(row["shortfall_quanta"]),
            )
            for row in settlements(*_TAX_PAYMENT_OBLIGATION_TYPES)
        ),
        *(
            TaxAccrualEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(
                    accrual_amount.get((row["month_index"], row["jurisdiction_id"]), row["total_tax_quanta"])
                ),
                jurisdiction_id=row["jurisdiction_id"],
                tax_year_end_month=row["tax_year_end_month"],
                ordinary_income_quanta=_quanta(row["ordinary_income_quanta"]),
                ltcg_quanta=_quanta(row["ltcg_quanta"]),
                stcg_quanta=_quanta(row["stcg_quanta"]),
                ordinary_tax_quanta=_quanta(row["ordinary_tax_quanta"]),
                capital_gain_tax_quanta=_quanta(row["capital_gain_tax_quanta"]),
                total_tax_quanta=_quanta(row["total_tax_quanta"]),
                mortgage_interest_deduction_quanta=_quanta(row["mortgage_interest_deduction_quanta"]),
                itemized_deduction_quanta=_quanta(row["itemized_deduction_quanta"]),
                standard_deduction_quanta=_quanta(row["standard_deduction_quanta"]),
            )
            for row in sorted(
                rows(events.tax_breakdowns, agent_id=primary_agent_id),
                key=lambda row: (row["month_index"], row["jurisdiction_id"]),
            )
        ),
        *(
            RolloutFailureEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["shortfall_quanta"]),
                amount_due_quanta=_quanta(row["amount_due_quanta"]),
                amount_paid_quanta=_quanta(row["amount_paid_quanta"]),
                shortfall_quanta=_quanta(row["shortfall_quanta"]),
            )
            for row in rows(events.rollout_failures, agent_id=primary_agent_id)
        ),
        *(
            SetRentedFractionMarkerEvent(
                month_index=row["month_index"],
                amount_quanta="0",
                property_id=row["property_id"],
                rented_fraction=row["rented_fraction"],
            )
            for row in sorted(
                rows(events.set_rented_fraction_events), key=lambda row: (row["month_index"], row["property_id"])
            )
        ),
        *(
            SetPrimaryResidenceMarkerEvent(
                month_index=row["month_index"],
                amount_quanta="0",
                agent_id=row["agent_id"],
                property_id=row["property_id"],
                is_primary_residence=row["is_primary_residence"],
            )
            for row in sorted(
                rows(events.set_primary_residence_events, agent_id=primary_agent_id),
                key=lambda row: (row["month_index"], row["agent_id"]),
            )
        ),
        *(
            CapitalImprovementMarkerEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["amount_quanta"]),
                property_id=row["property_id"],
            )
            for row in sorted(
                rows(events.capital_improvement_events), key=lambda row: (row["month_index"], row["property_id"])
            )
        ),
        *(
            PropertySaleMarkerEvent(
                month_index=row["month_index"],
                amount_quanta=_quanta(row["gross_proceeds_quanta"]),
                property_id=row["property_id"],
                gross_proceeds_quanta=_quanta(row["gross_proceeds_quanta"]),
                mortgage_payoff_quanta=_quanta(row["mortgage_payoff_quanta"]),
                net_cash_to_owner_quanta=_quanta(row["net_cash_to_owner_quanta"]),
                realized_gain_quanta=_quanta(row["realized_gain_quanta"]),
                depreciation_recapture_quanta=_quanta(row["depreciation_recapture_quanta"]),
                section_121_exclusion_quanta=_quanta(row["section_121_exclusion_quanta"]),
                long_term_capital_gain_quanta=_quanta(row["long_term_capital_gain_quanta"]),
            )
            for row in sorted(
                rows(events.property_sale_events), key=lambda row: (row["month_index"], row["property_id"])
            )
        ),
    ]
    return ProductRolloutProjection(
        currency_code=metrics.currency_code,
        currency_quantum=metrics.currency_quantum,
        monthly_metric_arrays=monthly_metric_arrays,
        failed_month_index=None if failed_month < 0 else failed_month,
        events=tuple(sorted(projected, key=lambda event: (event.month_index, _EVENT_PRIORITY[event.kind]))),
    )
