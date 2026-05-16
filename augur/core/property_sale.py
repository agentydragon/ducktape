from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from augur.core.local_regulation import LocalRegulation
from augur.core.market_bundle import MarketBundle
from augur.core.property_depreciation import monthly_property_depreciation_usd
from augur.core.scenario_set import PropertySaleEvent, Scenario, TaxFilingStatus

PRIMARY_RESIDENCE_CAPITAL_GAIN_EXCLUSION_USD: dict[TaxFilingStatus, float] = {
    TaxFilingStatus.SINGLE: 250_000.0,
    TaxFilingStatus.HEAD_OF_HOUSEHOLD: 250_000.0,
    TaxFilingStatus.MARRIED_FILING_JOINTLY: 500_000.0,
    TaxFilingStatus.MARRIED_FILING_SEPARATELY: 0.0,
}


@dataclass(frozen=True)
class PropertySaleSettlementArrays:
    gross_usd: np.ndarray
    selling_cost_usd: np.ndarray
    debt_payoff_usd: np.ndarray
    adjusted_basis_usd: np.ndarray
    realized_property_gain_usd: np.ndarray
    property_sale_capital_gain_usd: np.ndarray
    property_sale_capital_gain_exclusion_usd: np.ndarray
    taxable_property_capital_gain_usd: np.ndarray
    taxable_property_gain_usd: np.ndarray
    depreciation_recapture_usd: np.ndarray
    tax_usd: np.ndarray
    net_proceeds_usd: np.ndarray
    net_cash_flow_usd: np.ndarray


@dataclass(frozen=True)
class PropertyDispositionArrays:
    purchase_closing_cost_usd: np.ndarray
    property_depreciation_usd: np.ndarray
    cumulative_property_depreciation_usd: np.ndarray
    sale_settlement: PropertySaleSettlementArrays
    sale_event: PropertySaleEvent | None
    sale_month: int | None

    @property
    def sale_closing_cost_usd(self) -> np.ndarray:
        return self.sale_settlement.selling_cost_usd

    @property
    def property_sale_gross_usd(self) -> np.ndarray:
        return self.sale_settlement.gross_usd

    @property
    def property_sale_net_proceeds_usd(self) -> np.ndarray:
        return self.sale_settlement.net_proceeds_usd

    @property
    def property_sale_tax_usd(self) -> np.ndarray:
        return self.sale_settlement.tax_usd

    @property
    def property_sale_debt_payoff_usd(self) -> np.ndarray:
        return self.sale_settlement.debt_payoff_usd

    @property
    def property_sale_adjusted_basis_usd(self) -> np.ndarray:
        return self.sale_settlement.adjusted_basis_usd

    @property
    def realized_property_gain_usd(self) -> np.ndarray:
        return self.sale_settlement.realized_property_gain_usd

    @property
    def property_sale_capital_gain_usd(self) -> np.ndarray:
        return self.sale_settlement.property_sale_capital_gain_usd

    @property
    def property_sale_capital_gain_exclusion_usd(self) -> np.ndarray:
        return self.sale_settlement.property_sale_capital_gain_exclusion_usd

    @property
    def taxable_property_capital_gain_usd(self) -> np.ndarray:
        return self.sale_settlement.taxable_property_capital_gain_usd

    @property
    def taxable_property_gain_usd(self) -> np.ndarray:
        return self.sale_settlement.taxable_property_gain_usd

    @property
    def depreciation_recapture_usd(self) -> np.ndarray:
        return self.sale_settlement.depreciation_recapture_usd

    @property
    def net_property_sale_cash_flow_usd(self) -> np.ndarray:
        return self.sale_settlement.net_cash_flow_usd


def property_disposition_arrays(
    scenario: Scenario,
    market_bundle: MarketBundle,
    *,
    property_value_usd: np.ndarray,
    mortgage_balance_usd: np.ndarray,
    purchase_price_usd: float,
    local_regulation: LocalRegulation,
) -> PropertyDispositionArrays:
    shape = (market_bundle.rollout_count, market_bundle.horizon_months + 1)
    if scenario.property_selection.property_id is None:
        raise ValueError(f"scenario {scenario.scenario_id!r} has no real estate disposition")

    transaction_costs = scenario.transaction_costs
    tax_profile = scenario.tax_profile
    sale_event = property_sale_event(scenario)
    sale_month = property_sale_month(scenario, market_bundle.horizon_months)
    depreciation_through_month = sale_month if sale_month is not None else market_bundle.horizon_months
    purchase_closing_cost = np.zeros(shape, dtype="float64")
    purchase_closing_cost[:, 0] = purchase_price_usd * (transaction_costs.closing_cost_buy_pct / 100)
    property_depreciation = monthly_property_depreciation_usd(
        scenario,
        market_bundle,
        purchase_price_usd=purchase_price_usd,
        purchase_closing_cost_usd=float(purchase_closing_cost[0, 0]),
        sale_month=depreciation_through_month,
    )
    cumulative_depreciation = np.cumsum(property_depreciation, axis=1)

    sale_mask = np.zeros(shape, dtype="float64")
    if sale_month is not None:
        sale_mask[:, sale_month] = 1.0
    sale_gross = property_value_usd * sale_mask
    sale_closing_cost = sale_gross * (
        (transaction_costs.closing_cost_sell_pct + local_regulation.local_transfer_tax_pct) / 100
    )
    debt_payoff = mortgage_balance_usd * sale_mask
    accumulated_depreciation_at_sale = cumulative_depreciation * sale_mask

    cost_basis = purchase_price_usd + float(purchase_closing_cost[0, 0])
    adjusted_basis = (cost_basis - accumulated_depreciation_at_sale) * sale_mask
    realized_gain = np.maximum(0.0, sale_gross - sale_closing_cost - adjusted_basis) * sale_mask
    depreciation_recapture = np.minimum(accumulated_depreciation_at_sale, realized_gain)
    capital_gain = np.maximum(0.0, realized_gain - depreciation_recapture)
    exclusion_cap = PRIMARY_RESIDENCE_CAPITAL_GAIN_EXCLUSION_USD[tax_profile.filing_status]
    capital_gain_exclusion = np.minimum(capital_gain, exclusion_cap)
    taxable_capital_gain = np.maximum(0.0, capital_gain - capital_gain_exclusion)
    taxable_gain = depreciation_recapture + taxable_capital_gain
    # The engine owns sale-tax computation via annual_tax.annual_sale_tax_allocation
    # (bracket-aware federal + California). Disposition stops at pre-tax proceeds; the
    # tax obligation accrues and settles through the annual-tax obligation path.
    sale_tax = np.zeros_like(sale_gross)
    net_proceeds = sale_gross - sale_closing_cost - debt_payoff
    return PropertyDispositionArrays(
        purchase_closing_cost_usd=purchase_closing_cost,
        property_depreciation_usd=property_depreciation,
        cumulative_property_depreciation_usd=cumulative_depreciation,
        sale_settlement=PropertySaleSettlementArrays(
            gross_usd=sale_gross,
            selling_cost_usd=sale_closing_cost,
            debt_payoff_usd=debt_payoff,
            adjusted_basis_usd=adjusted_basis,
            realized_property_gain_usd=realized_gain,
            property_sale_capital_gain_usd=capital_gain,
            property_sale_capital_gain_exclusion_usd=capital_gain_exclusion,
            taxable_property_capital_gain_usd=taxable_capital_gain,
            taxable_property_gain_usd=taxable_gain,
            depreciation_recapture_usd=depreciation_recapture,
            tax_usd=sale_tax,
            net_proceeds_usd=net_proceeds,
            net_cash_flow_usd=net_proceeds,
        ),
        sale_event=sale_event,
        sale_month=sale_month,
    )


def empty_property_disposition_arrays(market_bundle: MarketBundle) -> PropertyDispositionArrays:
    zeros = np.zeros((market_bundle.rollout_count, market_bundle.horizon_months + 1), dtype="float64")
    return PropertyDispositionArrays(
        purchase_closing_cost_usd=zeros,
        property_depreciation_usd=zeros,
        cumulative_property_depreciation_usd=zeros,
        sale_settlement=PropertySaleSettlementArrays(
            gross_usd=zeros,
            selling_cost_usd=zeros,
            debt_payoff_usd=zeros,
            adjusted_basis_usd=zeros,
            realized_property_gain_usd=zeros,
            property_sale_capital_gain_usd=zeros,
            property_sale_capital_gain_exclusion_usd=zeros,
            taxable_property_capital_gain_usd=zeros,
            taxable_property_gain_usd=zeros,
            depreciation_recapture_usd=zeros,
            tax_usd=zeros,
            net_proceeds_usd=zeros,
            net_cash_flow_usd=zeros,
        ),
        sale_event=None,
        sale_month=None,
    )


def property_sale_month(scenario: Scenario, horizon_months: int) -> int | None:
    sale_event = property_sale_event(scenario)
    if sale_event is None:
        return None
    return max(0, min(int(sale_event.month_index), horizon_months))


def property_sale_event(scenario: Scenario) -> PropertySaleEvent | None:
    explicit_sale_events = [
        event
        for event in scenario.events
        if isinstance(event, PropertySaleEvent)
        and (event.property_id is None or event.property_id == scenario.property_selection.property_id)
    ]
    if not explicit_sale_events:
        return None
    return min(explicit_sale_events, key=lambda event: int(event.month_index))
