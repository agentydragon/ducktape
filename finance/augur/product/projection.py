"""Product rollout read model projected directly from compiled plan + dense output.

The analytics codec remains available through ``SimulationRun`` for consumers that need
long-form Polars frames.  Product rollout detail is a selected-trajectory read model: it
reads only one rollout's dense event output and the JAX-emitted product metric arrays,
without materializing broad state/event frames first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
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
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.enums import LifecycleKind, PrivateEquityOpportunityOutcome, TaxBreakdownChannel
from finance.augur.sim.output import DenseSimulationOutput
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


@dataclass(frozen=True)
class _ObligationEvent:
    month: int
    slot: int
    obligation_id: str
    obligation_type: ObligationType
    due: int
    paid: int
    shortfall: int
    failed: bool


@dataclass
class _HoldingSaleTotals:
    units: float = 0.0
    basis: int = 0
    proceeds: int = 0


def project_product_rollout(
    plan: CompiledSimulation,
    output: DenseSimulationOutput,
    metrics: ProductMetricArrays,
    *,
    rollout_index: int,
    primary_agent_id: str,
    asset_label_by_id: dict[str, str],
) -> ProductRolloutProjection:
    """Project one rollout directly from the engine's stable dense output contract."""

    if not 0 <= rollout_index < plan.rollout_count:
        raise IndexError(f"rollout_index {rollout_index} outside [0, {plan.rollout_count})")
    if metrics.currency_code != plan.currency_code or metrics.currency_quantum != format(plan.currency_quantum, "f"):
        raise ValueError("product metric currency metadata does not match the compiled simulation")

    monthly_metric_arrays = {
        name: values.copy() if name == "month_index" else values[:, rollout_index].copy()
        for name, values in metrics.metric_arrays().items()
    }
    failed_month = int(metrics.failed_month[rollout_index])
    primary_agent_code = _string_code(plan, primary_agent_id)
    obligations = _obligation_rows(plan, output, rollout_index=rollout_index, primary_agent_code=primary_agent_code)
    events = [
        *_holding_sale_events(
            plan,
            output,
            rollout_index=rollout_index,
            primary_agent_code=primary_agent_code,
            asset_label_by_id=asset_label_by_id,
        ),
        *_property_purchase_events(plan, output, rollout_index=rollout_index, primary_agent_code=primary_agent_code),
        *_private_equity_events(
            plan,
            rollout_index=rollout_index,
            primary_agent_code=primary_agent_code,
            asset_label_by_id=asset_label_by_id,
        ),
        *_private_equity_opportunity_events(
            plan,
            output,
            rollout_index=rollout_index,
            primary_agent_code=primary_agent_code,
            asset_label_by_id=asset_label_by_id,
        ),
        *_mortgage_payment_events(plan, output, rollout_index=rollout_index, primary_agent_code=primary_agent_code),
        *_payment_events(obligations, ObligationType.PROPERTY_TAX, PropertyTaxPaymentEvent),
        *_payment_events(obligations, ObligationType.HOA_DUES, HoaDuesPaymentEvent),
        *_payment_events(obligations, ObligationType.HOMEOWNERS_INSURANCE, HomeownersInsurancePaymentEvent),
        *_payment_events(obligations, ObligationType.PROPERTY_MAINTENANCE, PropertyMaintenancePaymentEvent),
        *_tax_accrual_events(plan, output, rollout_index=rollout_index, primary_agent_code=primary_agent_code),
        *_tax_payment_events(obligations),
        *_payment_events(obligations, ObligationType.CASH_SPEND, MonthlyExpenseEvent),
        *_payment_events(obligations, ObligationType.OUTSIDE_RENT, OutsideRentPaymentEvent),
        *_failure_events(obligations),
        *_set_rented_fraction_events(plan, output, rollout_index=rollout_index),
        *_set_primary_residence_events(
            plan, output, rollout_index=rollout_index, primary_agent_code=primary_agent_code
        ),
        *_capital_improvement_events(plan, output, rollout_index=rollout_index),
        *_property_sale_events(plan, output, rollout_index=rollout_index),
    ]
    return ProductRolloutProjection(
        currency_code=plan.currency_code,
        currency_quantum=format(plan.currency_quantum, "f"),
        monthly_metric_arrays=monthly_metric_arrays,
        failed_month_index=None if failed_month < 0 else failed_month,
        events=tuple(sorted(events, key=lambda event: (event.month_index, _EVENT_PRIORITY[event.kind]))),
    )


def _holding_sale_events(
    plan: CompiledSimulation,
    output: DenseSimulationOutput,
    *,
    rollout_index: int,
    primary_agent_code: int,
    asset_label_by_id: dict[str, str],
) -> tuple[RolloutEvent, ...]:
    # Current product behavior aggregates every disposition path by (month, asset).
    totals: dict[tuple[int, int], _HoldingSaleTotals] = {}

    def add(month: int, lot: int, units_quanta: int, basis: int, proceeds: int) -> None:
        asset_code = int(plan.lot_asset_codes[lot])
        row = totals.setdefault((month, asset_code), _HoldingSaleTotals())
        row.units += units_quanta / int(plan.lot_quantity_scale[lot])
        row.basis += basis
        row.proceeds += proceeds

    scheduled = output.scheduled_dispositions
    for sale, lot in np.argwhere(scheduled.active[:, :, rollout_index]):
        if int(plan.sales.agent[sale]) != primary_agent_code:
            continue
        add(
            int(plan.sales.month[sale]),
            int(lot),
            int(scheduled.units[sale, lot, rollout_index]),
            int(scheduled.basis[sale, lot, rollout_index]),
            int(scheduled.proceeds[sale, lot, rollout_index]),
        )

    target = output.target_allocation.dispositions
    for month, policy, sleeve, lot in np.argwhere(target.active[..., rollout_index]):
        if int(plan.target_allocation_policies.agent[policy]) != primary_agent_code:
            continue
        add(
            int(month),
            int(lot),
            int(target.units[month, policy, sleeve, lot, rollout_index]),
            int(target.basis[month, policy, sleeve, lot, rollout_index]),
            int(target.proceeds[month, policy, sleeve, lot, rollout_index]),
        )

    private_equity = output.private_equity.dispositions
    for month, _issuer, kind, lot in np.argwhere(private_equity.active[..., rollout_index]):
        if int(plan.lot_agent_codes[lot]) != primary_agent_code:
            continue
        add(
            int(month),
            int(lot),
            int(private_equity.units[month, _issuer, kind, lot, rollout_index]),
            int(private_equity.basis[month, _issuer, kind, lot, rollout_index]),
            int(private_equity.proceeds[month, _issuer, kind, lot, rollout_index]),
        )

    events: list[RolloutEvent] = []
    for (month, asset_code), totals_for_asset in sorted(
        totals.items(), key=lambda item: (item[0][0], plan.assets[item[0][1]].wire_id)
    ):
        asset = plan.assets[asset_code]
        events.append(
            HoldingSaleEvent(
                month_index=month,
                amount_quanta=_quanta(totals_for_asset.proceeds),
                asset=asset,
                asset_label=asset_label_by_id.get(asset.wire_id),
                units=totals_for_asset.units,
                proceeds_quanta=_quanta(totals_for_asset.proceeds),
                cost_basis_quanta=_quanta(totals_for_asset.basis),
            )
        )
    return tuple(events)


def _property_purchase_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int, primary_agent_code: int
) -> tuple[RolloutEvent, ...]:
    events: list[RolloutEvent] = []
    for month, prop in np.argwhere(output.property_purchases[:, :, rollout_index]):
        if int(plan.properties.buyer_agent[prop]) != primary_agent_code:
            continue
        mortgage_principal = 0
        liability = int(plan.properties.mortgage_slot[prop])
        if liability >= 0 and output.mortgages.origination_active[month, liability, rollout_index]:
            mortgage_principal = int(plan.liabilities.principal[liability])
        property_id = _required_text(plan, int(plan.properties.id[prop]))
        purchase_price = int(plan.properties.purchase_price[prop])
        events.append(
            PropertyPurchaseEvent(
                month_index=int(month),
                amount_quanta=_quanta(purchase_price),
                property_id=property_id,
                purchase_price_quanta=_quanta(purchase_price),
                down_payment_quanta=_quanta(plan.properties.equity_ledger[prop]),
                mortgage_principal_quanta=_quanta(mortgage_principal),
            )
        )
        closing_cost = int(plan.properties.closing_cost[prop])
        if closing_cost > 0:
            events.append(
                ClosingCostPaymentEvent(
                    month_index=int(month), amount_quanta=_quanta(closing_cost), property_id=property_id
                )
            )
    return tuple(events)


def _private_equity_events(
    plan: CompiledSimulation, *, rollout_index: int, primary_agent_code: int, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    primary_issuers = _primary_private_equity_issuers(plan, primary_agent_code)
    channels = plan.pe_channels.execution
    rows: list[tuple[int, str, PrivateEquityMarkerEvent]] = []
    for issuer_index, issuer_id in enumerate(plan.pe_issuers.issuer_ids):
        if issuer_id not in primary_issuers:
            continue
        asset = PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id))
        for month in range(plan.horizon_months):
            event_kind = PrivateEquityEventKindCode(
                int(plan.pe_channels.event_kind_codes[issuer_index, rollout_index, month])
            )
            if event_kind == PrivateEquityEventKindCode.NONE:
                continue
            regime = PrivateEquityRegimeCode(int(channels.regime_codes[issuer_index, rollout_index, month]))
            rows.append(
                (
                    month,
                    issuer_id,
                    PrivateEquityMarkerEvent(
                        month_index=month,
                        amount_quanta="0",
                        issuer_id=issuer_id,
                        asset=asset,
                        asset_label=asset_label_by_id.get(asset.wire_id),
                        event_kind=event_kind.name.lower(),
                        regime=regime.name.lower(),
                        mark_quanta=_quanta(channels.mark_quanta[issuer_index, rollout_index, month]),
                        sale_capacity_fraction=float(
                            channels.sale_capacity_fractions[issuer_index, rollout_index, month]
                        ),
                        eligible_fraction=float(channels.eligible_fractions[issuer_index, rollout_index, month]),
                        forced_sale_fraction=float(channels.forced_sale_fractions[issuer_index, rollout_index, month]),
                        liquidity_blocked=bool(channels.liquidity_blocked[issuer_index, rollout_index, month]),
                        forced_recovery_cashout_quanta=_quanta(
                            channels.forced_recovery_cashout_quanta[issuer_index, rollout_index, month]
                        ),
                    ),
                )
            )
    return tuple(row[2] for row in sorted(rows, key=lambda row: (row[0], row[1], row[2].event_kind)))


def _private_equity_opportunity_events(
    plan: CompiledSimulation,
    output: DenseSimulationOutput,
    *,
    rollout_index: int,
    primary_agent_code: int,
    asset_label_by_id: dict[str, str],
) -> tuple[RolloutEvent, ...]:
    primary_issuers = _primary_private_equity_issuers(plan, primary_agent_code)
    channels = plan.pe_channels.execution
    rows: list[tuple[int, str, PrivateEquityOpportunityEvent]] = []
    for month, issuer_index in np.argwhere(output.private_equity.opportunities.active[:, :, rollout_index]):
        issuer_id = plan.pe_issuers.issuer_ids[int(issuer_index)]
        if issuer_id not in primary_issuers:
            continue
        asset = PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id))
        event_kind = PrivateEquityEventKindCode(
            int(plan.pe_channels.event_kind_codes[issuer_index, rollout_index, month])
        )
        regime = PrivateEquityRegimeCode(int(channels.regime_codes[issuer_index, rollout_index, month]))
        outcome = PrivateEquityOpportunityOutcome(
            int(output.private_equity.opportunities.outcome[month, issuer_index, rollout_index])
        )
        scale = _private_equity_scale(plan, int(issuer_index))
        event = PrivateEquityOpportunityEvent(
            month_index=int(month),
            amount_quanta=_quanta(output.private_equity.opportunities.proceeds[month, issuer_index, rollout_index]),
            issuer_id=issuer_id,
            asset=asset,
            asset_label=asset_label_by_id.get(asset.wire_id),
            event_kind=event_kind.name.lower(),
            regime=regime.name.lower(),
            outcome=outcome.name.lower(),
            mark_quanta=_quanta(channels.mark_quanta[issuer_index, rollout_index, month]),
            sale_capacity_fraction=float(channels.sale_capacity_fractions[issuer_index, rollout_index, month]),
            eligible_fraction=float(channels.eligible_fractions[issuer_index, rollout_index, month]),
            liquidity_blocked=bool(channels.liquidity_blocked[issuer_index, rollout_index, month]),
            floor_quanta=_quanta(output.private_equity.opportunities.floor[month, issuer_index, rollout_index]),
            liquid_net_worth_quanta=_quanta(
                output.private_equity.opportunities.liquid_net_worth[month, issuer_index, rollout_index]
            ),
            shortfall_quanta=_quanta(output.private_equity.opportunities.shortfall[month, issuer_index, rollout_index]),
            units_held=float(output.private_equity.opportunities.units_held[month, issuer_index, rollout_index])
            / scale,
            sellable_units=float(output.private_equity.opportunities.sellable_units[month, issuer_index, rollout_index])
            / scale,
            target_units=float(output.private_equity.opportunities.target_units[month, issuer_index, rollout_index])
            / scale,
            proceeds_quanta=_quanta(output.private_equity.opportunities.proceeds[month, issuer_index, rollout_index]),
        )
        rows.append((int(month), issuer_id, event))
    return tuple(row[2] for row in sorted(rows, key=lambda row: (row[0], row[1], row[2].outcome)))


def _mortgage_payment_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int, primary_agent_code: int
) -> tuple[RolloutEvent, ...]:
    events: list[RolloutEvent] = []
    for month, liability in np.argwhere(output.mortgages.payment_active[:, :, rollout_index]):
        if int(plan.liabilities.agent[liability]) != primary_agent_code:
            continue
        events.append(
            MortgagePaymentEvent(
                month_index=int(month),
                amount_quanta=_quanta(output.mortgages.payment_total[month, liability, rollout_index]),
                interest_quanta=_quanta(output.mortgages.payment_interest[month, liability, rollout_index]),
                principal_quanta=_quanta(output.mortgages.payment_principal[month, liability, rollout_index]),
            )
        )
    return tuple(events)


def _tax_accrual_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int, primary_agent_code: int
) -> tuple[RolloutEvent, ...]:
    events: list[TaxAccrualEvent] = []
    breakdown = output.taxes.breakdown[:, :, :, rollout_index]
    for month, link in np.argwhere(breakdown[TaxBreakdownChannel.ACCRUAL_ACTIVE] > 0):
        profile = int(plan.tax.link_profile[link])
        if int(plan.tax.profile_agent[profile]) != primary_agent_code:
            continue
        events.append(
            TaxAccrualEvent(
                month_index=int(month),
                amount_quanta=_quanta(
                    breakdown[TaxBreakdownChannel.ORDINARY_TAX, month, link]
                    + breakdown[TaxBreakdownChannel.CAPITAL_GAIN_TAX, month, link]
                ),
                jurisdiction_id=_required_text(plan, int(plan.tax.link_jurisdiction[link])),
                tax_year_end_month=int(month),
                ordinary_income_quanta=_quanta(breakdown[TaxBreakdownChannel.ORDINARY_INCOME, month, link]),
                ltcg_quanta=_quanta(breakdown[TaxBreakdownChannel.LTCG, month, link]),
                stcg_quanta=_quanta(breakdown[TaxBreakdownChannel.STCG, month, link]),
                ordinary_tax_quanta=_quanta(breakdown[TaxBreakdownChannel.ORDINARY_TAX, month, link]),
                capital_gain_tax_quanta=_quanta(breakdown[TaxBreakdownChannel.CAPITAL_GAIN_TAX, month, link]),
                total_tax_quanta=_quanta(
                    breakdown[TaxBreakdownChannel.ORDINARY_TAX, month, link]
                    + breakdown[TaxBreakdownChannel.CAPITAL_GAIN_TAX, month, link]
                ),
                mortgage_interest_deduction_quanta=_quanta(
                    breakdown[TaxBreakdownChannel.MORTGAGE_DEDUCTION, month, link]
                ),
                itemized_deduction_quanta=_quanta(breakdown[TaxBreakdownChannel.ITEMIZED_DEDUCTION, month, link]),
                standard_deduction_quanta=_quanta(plan.tax.link_standard_deduction[link]),
            )
        )
    return tuple(sorted(events, key=lambda event: (event.month_index, event.jurisdiction_id)))


def _obligation_rows(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int, primary_agent_code: int
) -> tuple[_ObligationEvent, ...]:
    rows: list[_ObligationEvent] = []
    for month, slot in np.argwhere(output.obligations.active[:, :, rollout_index]):
        if int(plan.obligations.metadata.agent[month, slot]) != primary_agent_code:
            continue
        rows.append(
            _ObligationEvent(
                month=int(month),
                slot=int(slot),
                obligation_id=_required_text(plan, int(plan.obligations.metadata.id[month, slot])),
                obligation_type=ObligationType(_required_text(plan, int(plan.obligations.metadata.type[month, slot]))),
                due=int(output.obligations.due[month, slot, rollout_index]),
                paid=int(output.obligations.paid[month, slot, rollout_index]),
                shortfall=int(output.obligations.shortfall[month, slot, rollout_index]),
                failed=bool(output.obligations.failure_active[month, slot, rollout_index]),
            )
        )
    return tuple(rows)


def _payment_events(
    obligations: tuple[_ObligationEvent, ...], obligation_type: ObligationType, event_type: type[Any]
) -> tuple[RolloutEvent, ...]:
    return tuple(
        event_type(
            month_index=row.month,
            amount_quanta=_quanta(row.paid),
            amount_due_quanta=_quanta(row.due),
            amount_paid_quanta=_quanta(row.paid),
            shortfall_quanta=_quanta(row.shortfall),
        )
        for row in sorted(
            (row for row in obligations if row.obligation_type == obligation_type),
            key=lambda row: (row.month, row.obligation_id),
        )
    )


def _tax_payment_events(obligations: tuple[_ObligationEvent, ...]) -> tuple[RolloutEvent, ...]:
    return tuple(
        TaxPaymentEvent(
            month_index=row.month,
            amount_quanta=_quanta(row.paid),
            obligation_type=row.obligation_type.value,
            amount_due_quanta=_quanta(row.due),
            amount_paid_quanta=_quanta(row.paid),
            shortfall_quanta=_quanta(row.shortfall),
        )
        for row in sorted(
            (row for row in obligations if row.obligation_type in _TAX_PAYMENT_OBLIGATION_TYPES),
            key=lambda row: (row.month, row.obligation_id),
        )
    )


def _failure_events(obligations: tuple[_ObligationEvent, ...]) -> tuple[RolloutEvent, ...]:
    return tuple(
        RolloutFailureEvent(
            month_index=row.month,
            amount_quanta=_quanta(row.shortfall),
            amount_due_quanta=_quanta(row.due),
            amount_paid_quanta=_quanta(row.paid),
            shortfall_quanta=_quanta(row.shortfall),
        )
        for row in obligations
        if row.failed
    )


def _set_rented_fraction_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int
) -> tuple[RolloutEvent, ...]:
    events: list[SetRentedFractionMarkerEvent] = []
    event_count = int(plan.lifecycle_events.month.shape[0])
    for event_index in np.flatnonzero(output.lifecycle.fired[:event_count, rollout_index]):
        if int(plan.lifecycle_events.kind[event_index]) != LifecycleKind.FRACTION:
            continue
        prop = int(plan.lifecycle_events.property_slot[event_index])
        events.append(
            SetRentedFractionMarkerEvent(
                month_index=int(plan.lifecycle_events.month[event_index]),
                amount_quanta="0",
                property_id=_required_text(plan, int(plan.properties.id[prop])),
                rented_fraction=float(plan.lifecycle_events.rented_fraction[event_index]),
            )
        )
    return tuple(sorted(events, key=lambda event: (event.month_index, event.property_id)))


def _set_primary_residence_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int, primary_agent_code: int
) -> tuple[RolloutEvent, ...]:
    events: list[SetPrimaryResidenceMarkerEvent] = []
    event_count = int(plan.primary_residence_events.month.shape[0])
    for event_index in np.flatnonzero(output.primary_residence_fired[:event_count, rollout_index]):
        agent_slot = int(plan.primary_residence_events.agent_slot[event_index])
        if int(plan.agent_codes[agent_slot]) != primary_agent_code:
            continue
        property_slot = int(plan.primary_residence_events.property_slot[event_index])
        events.append(
            SetPrimaryResidenceMarkerEvent(
                month_index=int(plan.primary_residence_events.month[event_index]),
                amount_quanta="0",
                agent_id=_required_text(plan, primary_agent_code),
                property_id=(
                    None if property_slot < 0 else _required_text(plan, int(plan.properties.id[property_slot]))
                ),
                is_primary_residence=property_slot >= 0,
            )
        )
    return tuple(sorted(events, key=lambda event: (event.month_index, event.agent_id)))


def _capital_improvement_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int
) -> tuple[RolloutEvent, ...]:
    events: list[CapitalImprovementMarkerEvent] = []
    event_count = int(plan.lifecycle_events.month.shape[0])
    for event_index in np.flatnonzero(output.lifecycle.fired[:event_count, rollout_index]):
        if int(plan.lifecycle_events.kind[event_index]) != LifecycleKind.CAPITAL_IMPROVEMENT:
            continue
        prop = int(plan.lifecycle_events.property_slot[event_index])
        events.append(
            CapitalImprovementMarkerEvent(
                month_index=int(plan.lifecycle_events.month[event_index]),
                amount_quanta=_quanta(plan.lifecycle_events.amount_quanta[event_index]),
                property_id=_required_text(plan, int(plan.properties.id[prop])),
            )
        )
    return tuple(sorted(events, key=lambda event: (event.month_index, event.property_id)))


def _property_sale_events(
    plan: CompiledSimulation, output: DenseSimulationOutput, *, rollout_index: int
) -> tuple[RolloutEvent, ...]:
    events: list[PropertySaleMarkerEvent] = []
    event_count = int(plan.lifecycle_events.month.shape[0])
    for event_index in np.flatnonzero(output.lifecycle.fired[:event_count, rollout_index]):
        if int(plan.lifecycle_events.kind[event_index]) != LifecycleKind.SALE:
            continue
        prop = int(plan.lifecycle_events.property_slot[event_index])
        events.append(
            PropertySaleMarkerEvent(
                month_index=int(plan.lifecycle_events.month[event_index]),
                amount_quanta=_quanta(output.lifecycle.property_sales.gross_proceeds[event_index, rollout_index]),
                property_id=_required_text(plan, int(plan.properties.id[prop])),
                gross_proceeds_quanta=_quanta(
                    output.lifecycle.property_sales.gross_proceeds[event_index, rollout_index]
                ),
                mortgage_payoff_quanta=_quanta(
                    output.lifecycle.property_sales.mortgage_payoff[event_index, rollout_index]
                ),
                net_cash_to_owner_quanta=_quanta(output.lifecycle.property_sales.net_cash[event_index, rollout_index]),
                realized_gain_quanta=_quanta(output.lifecycle.property_sales.realized_gain[event_index, rollout_index]),
                depreciation_recapture_quanta=_quanta(
                    output.lifecycle.property_sales.depreciation_recapture[event_index, rollout_index]
                ),
                section_121_exclusion_quanta=_quanta(
                    output.lifecycle.property_sales.section_121_exclusion[event_index, rollout_index]
                ),
                long_term_capital_gain_quanta=_quanta(
                    output.lifecycle.property_sales.long_term_capital_gain[event_index, rollout_index]
                ),
            )
        )
    return tuple(sorted(events, key=lambda event: (event.month_index, event.property_id)))


def _primary_private_equity_issuers(plan: CompiledSimulation, primary_agent_code: int) -> frozenset[str]:
    issuers: set[str] = set()
    for lot, asset_code in enumerate(plan.lot_asset_codes):
        if int(plan.lot_agent_codes[lot]) != primary_agent_code:
            continue
        asset = plan.assets[int(asset_code)]
        if isinstance(asset, PrivateEquityAssetKey):
            issuers.add(str(asset.issuer_id))
    return frozenset(issuers)


def _private_equity_scale(plan: CompiledSimulation, issuer_index: int) -> int:
    lots = np.flatnonzero(plan.pe_issuers.lot_mask[issuer_index])
    return int(plan.lot_quantity_scale[int(lots[0])]) if lots.size else 1


def _string_code(plan: CompiledSimulation, value: str) -> int:
    try:
        return plan.strings.index(value)
    except ValueError as exc:
        raise ValueError(f"compiled simulation has no string code for {value!r}") from exc


def _required_text(plan: CompiledSimulation, code: int) -> str:
    if code < 0:
        raise ValueError("required compiled string code is absent")
    return plan.strings[code]


def _quanta(value: int | np.integer[Any]) -> str:
    return str(int(value))
