"""Adapt the canonical integer fixture to the existing Python/JAX simulator."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, cast

import numpy as np

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import quantity_scale_for_asset
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    BondHolding,
    CapitalImprovementEvent,
    DistributionTaxSlice,
    FederalSaltCapEntry,
    FederalSaltDeductionPolicy,
    FixedAmount,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PrivateEquityTenderPolicy,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate_with_external_series
from finance.augur.sim.tlh_harvest import HarvestYieldParams


def _money(quanta: int, quantum: str) -> Decimal:
    return Decimal(quanta) * Decimal(quantum)


def _amount(spec: int | dict[str, Any], quantum: str) -> Decimal | FixedAmount | SeriesIndexedAmount:
    if isinstance(spec, int):
        return _money(spec, quantum)
    match spec.get("kind"):
        case "fixed":
            return FixedAmount(amount=_money(cast(int, spec["amount"]), quantum))
        case "series_indexed":
            series_id = cast(str, spec["series_id"])
            if series_id == "inflation":
                series: IndexSeriesKey = InflationKey()
            elif series_id.startswith("rent:") and (location_id := series_id.removeprefix("rent:")):
                series = RentKey(location_id=LocationId(location_id))
            else:
                raise ValueError(f"unsupported amount index series {series_id!r}")
            return SeriesIndexedAmount(
                base_amount=_money(cast(int, spec["base_amount"]), quantum),
                series=series,
                base_month_index=cast(int, spec.get("base_month_index", 0)),
                adjustment_period_months=cast(int, spec.get("adjustment_period_months", 1)),
            )
        case kind:
            raise ValueError(f"unsupported amount kind {kind!r}")


def _amount_schedule(spec: int | dict[str, Any], quantum: str) -> FixedAmount | SeriesIndexedAmount:
    amount = _amount(spec, quantum)
    return FixedAmount(amount=amount) if isinstance(amount, Decimal) else amount


def _sleeve_target(spec: dict[str, Any]) -> SleeveTarget:
    asset = SecurityKey(symbol=SecuritySymbol(spec["asset_id"]))
    expected_scale = quantity_scale_for_asset(asset)
    if spec["quantity_scale"] != expected_scale:
        raise ValueError(
            f"target-allocation sleeve {spec['asset_id']!r} uses quantity scale {spec['quantity_scale']}, "
            f"but the canonical Python asset scale is {expected_scale}"
        )
    return SleeveTarget(asset=asset, weight=spec["weight"])


def _asset(asset_id: str) -> SecurityKey | PrivateEquityAssetKey:
    if asset_id.startswith("private_equity:") and (issuer_id := asset_id.removeprefix("private_equity:")):
        return PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id))
    return SecurityKey(symbol=SecuritySymbol(asset_id))


def _ppb_float(value: int, *, context: str) -> float:
    factor = value / 1_000_000_000
    reconstructed = math.floor(abs(factor) * 1_000_000_000 + 0.5)
    reconstructed = reconstructed if value >= 0 else -reconstructed
    if abs(value) > 1 << 53 or reconstructed != value:
        raise ValueError(f"{context} {value} ppb cannot round-trip exactly through the Python/JAX float boundary")
    return factor


def _bond_coupon_rate(spec: dict[str, Any]) -> float:
    rate_ppb = cast(int, spec["annual_coupon_rate_ppb"])
    rate = _ppb_float(rate_ppb, context=f"bond {spec['bond_id']!r} coupon rate")
    if spec.get("inflation_indexed", False):
        period = cast(int, spec.get("coupon_period_months", 6))
        numerator = rate_ppb * period
        exact_period_rate_ppb = (2 * numerator + 12) // 24
        legacy_period_rate_ppb = math.floor(rate * period / 12 * 1_000_000_000 + 0.5)
        if exact_period_rate_ppb != legacy_period_rate_ppb:
            raise ValueError(
                f"indexed bond {spec['bond_id']!r} period rate cannot match the Python/JAX float boundary exactly"
            )
    return rate


def build_legacy_fixture(fixture: dict[str, Any]) -> tuple[Scenario, ExternalSeriesContext, dict[str, Location]]:
    """Build existing-simulator inputs from one strict integer fixture.

    Conversion to legacy floats occurs only for the old quantity and external
    level surfaces. Money remains exact Decimal at the adapter boundary and is
    quantized by the existing simulator's fixed-point compiler.
    """

    quantum = cast(str, fixture["currency_quantum"])
    scenario_spec = cast(dict[str, Any], fixture["scenario"])
    account_specs = cast(list[dict[str, Any]], scenario_spec["accounts"])
    agents = sorted({cast(str, spec["account"]["agent_id"]) for spec in account_specs})

    rollout_count = cast(int, fixture["rollout_count"])
    lots = cast(list[dict[str, Any]], scenario_spec["initial_lots"])
    sales = cast(list[dict[str, Any]], scenario_spec["scheduled_sales"])
    level_blocks: list[tuple[LevelSeriesKey, Any]] = []
    price_matrices: dict[str, np.ndarray[Any, np.dtype[np.int64]]] = {}
    pe_channels: dict[str, dict[str, np.ndarray[Any, Any]]] = {}
    for series in fixture["series"]:
        series_id = cast(str, series["series_id"])
        if series_id.startswith("security:"):
            asset_id = series_id.removeprefix("security:")
            key: LevelSeriesKey = SecurityKey(symbol=SecuritySymbol(asset_id))
        elif series_id.startswith("security_distribution:"):
            asset_id = series_id.removeprefix("security_distribution:")
            key = SecurityDistributionKey(symbol=SecuritySymbol(asset_id))
        elif series_id.startswith("home_value:"):
            location_id = series_id.removeprefix("home_value:")
            key = HomeValueKey(location_id=LocationId(location_id))
        elif series_id == "inflation":
            key = InflationKey()
        elif series_id.startswith("rent:") and (location_id := series_id.removeprefix("rent:")):
            key = RentKey(location_id=LocationId(location_id))
        elif series_id.startswith("private_equity_"):
            channel_and_issuer = series_id.removeprefix("private_equity_")
            channel, separator, issuer_id = channel_and_issuer.partition(":")
            if not separator or not issuer_id:
                raise ValueError(f"invalid private-equity fixture series {series_id!r}")
            snapshots = cast(int, series["snapshots"])
            raw = np.asarray(series["values"], dtype=np.int64).reshape(rollout_count, snapshots)
            pe_channels.setdefault(issuer_id, {})[channel] = raw
            continue
        else:
            continue
        snapshots = cast(int, series["snapshots"])
        price_matrix_quanta = np.asarray(series["values"], dtype=np.int64).reshape(rollout_count, snapshots)
        if isinstance(key, (InflationKey, RentKey)):
            level_values: list[float] = []
            for raw_value in price_matrix_quanta.flat:
                value = int(raw_value)
                if value <= 0:
                    raise ValueError(f"amount index series {series_id!r} has non-positive level {value}")
                level = value / 1_000_000_000
                reconstructed = math.floor(level * 1_000_000_000 + 0.5)
                if value > 1 << 53 or reconstructed != value:
                    raise ValueError(
                        f"amount index series {series_id!r} level {value} cannot round-trip exactly "
                        "through the Python/JAX float level boundary"
                    )
                level_values.append(level)
            price_matrix = np.asarray(level_values, dtype=np.float64).reshape(rollout_count, snapshots)
        else:
            price_matrix = price_matrix_quanta.astype(np.float64) * float(Decimal(quantum))
        if isinstance(key, SecurityKey):
            price_matrices[asset_id] = price_matrix_quanta
        level_blocks.append((key, price_matrix))

    pool_scales: dict[tuple[str, str, str], int] = {}
    for lot in lots:
        pool = (lot["agent_id"], lot["account_id"], lot["asset_id"])
        scale = cast(int, lot["quantity_scale"])
        previous = pool_scales.setdefault(pool, scale)
        if previous != scale:
            raise ValueError(f"mixed quantity scales for FIFO pool {pool!r}")
        if cast(int, lot["basis"]) * scale % cast(int, lot["units"]):
            raise ValueError(
                f"lot {lot['lot_id']!r} total basis does not encode an exact integer-quantum per-unit basis"
            )

    def sale_price(spec: dict[str, Any]) -> Decimal:
        prices = price_matrices[cast(str, spec["asset_id"])][:, cast(int, spec["month"])]
        if np.unique(prices).size != 1:
            raise ValueError(
                f"legacy ScheduledAssetSale requires one fixed price across rollouts for {spec['cause_id']!r}"
            )
        return _money(int(prices[0]), quantum)

    mortgage_principal_by_liability = {
        cast(str, mortgage["liability_id"]): _money(cast(int, mortgage["principal"]), quantum)
        for purchase in scenario_spec.get("scheduled_property_purchases", [])
        if (mortgage := purchase.get("mortgage")) is not None
    }
    tax_jurisdiction_ids = {
        cast(str, rules["jurisdiction_id"])
        for profile in scenario_spec.get("tax_profiles", [])
        for rules in profile["jurisdictions"]
    }

    scenario = Scenario(
        agents=[Agent(agent_id=agent_id) for agent_id in agents],
        initial_cash=[
            InitialAccountBalance(
                agent_id=spec["account"]["agent_id"],
                account_id=spec["account"]["account_id"],
                balance=_money(spec["opening_balance"], quantum),
            )
            for spec in account_specs
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=spec["month"],
                cause_id=spec["cause_id"],
                from_agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount=_amount(spec["amount"], quantum),
                income_category=ORDINARY_INCOME if spec.get("income_category") == "ordinary" else None,
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec["scheduled_transfers"]
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=spec["start_month"],
                end_month=spec["end_month"],
                cause_id=spec["cause_id"],
                from_agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount=_amount(spec["amount"], quantum),
                income_category=ORDINARY_INCOME if spec.get("income_category") == "ordinary" else None,
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec["recurring_transfers"]
        ],
        scheduled_property_cashflows=[
            ScheduledPropertyCashflow(
                month=spec["month"],
                property_id=spec["property_id"],
                cause_id=spec["cause_id"],
                from_agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount=_amount(spec["amount"], quantum),
                income_category=ORDINARY_INCOME if spec.get("income_category") == "ordinary" else None,
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec.get("scheduled_property_cashflows", [])
        ],
        recurring_property_cashflows=[
            RecurringPropertyCashflow(
                start_month=spec["start_month"],
                end_month=spec["end_month"],
                property_id=spec["property_id"],
                cause_id=spec["cause_id"],
                from_agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount=_amount(spec["amount"], quantum),
                income_category=ORDINARY_INCOME if spec.get("income_category") == "ordinary" else None,
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec.get("recurring_property_cashflows", [])
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=spec["month"],
                obligation_id=spec["obligation_id"],
                obligation_type=spec.get("obligation_type", "cash_spend"),
                agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount_due=_amount(spec["amount_due"], quantum),
                property_id=spec.get("property_id"),
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec["obligations"]
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=spec["start_month"],
                end_month=spec["end_month"],
                obligation_id=spec["obligation_id"],
                obligation_type=spec.get("obligation_type", "cash_spend"),
                agent_id=spec["from"]["agent_id"],
                from_account_id=spec["from"]["account_id"],
                to_agent_id=spec["to"]["agent_id"],
                to_account_id=spec["to"]["account_id"],
                amount_due=_amount(spec["amount_due"], quantum),
                property_id=spec.get("property_id"),
                deduction_category="ordinary" if spec.get("deduction_category") == "ordinary" else None,
            )
            for spec in scenario_spec.get("recurring_obligations", [])
        ],
        initial_lots=[
            InitialLot(
                lot_id=spec["lot_id"],
                agent_id=spec["agent_id"],
                account_id=spec["account_id"],
                asset=_asset(spec["asset_id"]),
                purchase_month_index=spec["purchase_month"],
                quantity=spec["units"] / spec["quantity_scale"],
                cost_basis_per_unit=_money(spec["basis"] * spec["quantity_scale"] // spec["units"], quantum),
            )
            for spec in lots
        ],
        initial_bonds=[
            BondHolding(
                bond_id=spec["bond_id"],
                agent_id=spec["agent_id"],
                account_id=spec["account_id"],
                issuer_jurisdiction_id=spec.get("issuer_jurisdiction_id"),
                face_value=_money(spec["face_value"], quantum),
                purchase_price=_money(spec["purchase_price"], quantum),
                annual_coupon_rate=_bond_coupon_rate(spec),
                coupon_period_months=spec.get("coupon_period_months", 6),
                inflation_indexed=spec.get("inflation_indexed", False),
                purchase_month_index=spec["purchase_month_index"],
                maturity_month_index=spec["maturity_month_index"],
            )
            for spec in scenario_spec.get("initial_bonds", [])
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=spec["month"],
                cause_id=spec["cause_id"],
                agent_id=spec["agent_id"],
                source_account_id=spec["account_id"],
                asset=_asset(spec["asset_id"]),
                quantity=spec["units"] / pool_scales[(spec["agent_id"], spec["account_id"], spec["asset_id"])],
                proceeds_account_id=spec["proceeds_account_id"],
                price_per_unit=sale_price(spec),
            )
            for spec in sales
        ],
        security_distributions=[
            SecurityDistribution(
                asset=SecurityKey(symbol=SecuritySymbol(spec["asset_id"])),
                agent_id=spec["agent_id"],
                holding_account_id=spec["holding_account_id"],
                to_account_id=spec["to_account_id"],
                tax_character=tuple(
                    DistributionTaxSlice(
                        fraction=_ppb_float(
                            tax_slice["fraction_ppb"],
                            context=(
                                f"distribution {spec['agent_id']}/{spec['holding_account_id']}/"
                                f"{spec['asset_id']} tax fraction"
                            ),
                        ),
                        issuer_jurisdiction_id=tax_slice.get("issuer_jurisdiction_id"),
                    )
                    for tax_slice in spec.get(
                        "tax_character", [{"fraction_ppb": 1_000_000_000, "issuer_jurisdiction_id": None}]
                    )
                ),
            )
            for spec in scenario_spec.get("distributions", [])
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id=spec["agent_id"],
                account_id=spec["account_id"],
                source_account_ids=tuple(spec.get("source_account_ids", [])),
                sleeves=[_sleeve_target(sleeve) for sleeve in spec["sleeves"]],
                cash_floor=_amount(spec.get("cash_floor", 0), quantum),
                cash_ceiling=_amount(spec["cash_ceiling"], quantum),
                cause_id_prefix=spec.get("cause_id_prefix", "allocation_sale"),
                purchase_slots_per_sleeve=spec.get("purchase_slots_per_sleeve", 0),
                rebalance_tolerance=(
                    None
                    if spec.get("rebalance_tolerance_ppb") is None
                    else _ppb_float(
                        spec["rebalance_tolerance_ppb"],
                        context=f"target-allocation {spec['agent_id']}/{spec['account_id']} tolerance",
                    )
                ),
            )
            for spec in scenario_spec.get("target_allocation_policies", [])
        ],
        private_equity_tender_policies=[
            PrivateEquityTenderPolicy(
                owner_agent_id=spec["owner_agent_id"],
                proceeds_account_id=spec.get("proceeds_account_id", "checking"),
                liquid_net_worth_floor=_amount_schedule(spec["liquid_net_worth_floor"], quantum),
            )
            for spec in scenario_spec.get("private_equity_tender_policies", [])
        ],
        harvest_policies=[
            HarvestPolicy(
                owner_agent_id=spec["owner_agent_id"],
                account_id=spec.get("account_id", "checking"),
                asset=_asset(spec["asset_id"]),
                yield_params=HarvestYieldParams(
                    peak_annual_yield=spec["peak_annual_yield_ppb"] / 1_000_000_000,
                    floor_annual_yield=spec["floor_annual_yield_ppb"] / 1_000_000_000,
                    maturity_decay_exponent=spec["maturity_decay_exponent_ppb"] / 1_000_000_000,
                    drawdown_sensitivity=spec["drawdown_sensitivity_ppb"] / 1_000_000_000,
                ),
                short_term_fraction=spec.get("short_term_fraction_ppb", 1_000_000_000) / 1_000_000_000,
            )
            for spec in scenario_spec.get("harvest_policies", [])
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=spec["month"],
                cause_id=spec["cause_id"],
                property_id=spec["property_id"],
                location_id=spec["location_id"],
                buyer_agent_id=spec["buyer_agent_id"],
                buyer_account_id=spec["buyer_account_id"],
                seller_agent_id=spec["seller_agent_id"],
                seller_account_id=spec.get("seller_account_id", "checking"),
                purchase_price=_money(spec["purchase_price"], quantum),
                down_payment=_money(spec["down_payment"], quantum),
                buyer_closing_cost=_money(spec.get("buyer_closing_cost", 0), quantum),
                rented_fraction=spec.get("rented_fraction_ppb", 0) / 1_000_000_000,
                land_value_fraction=spec.get("land_value_fraction_ppb", 200_000_000) / 1_000_000_000,
                mortgage=(
                    MortgageFinancing(
                        liability_id=spec["mortgage"]["liability_id"],
                        lender_agent_id=spec["mortgage"]["lender_agent_id"],
                        lender_account_id=spec["mortgage"].get("lender_account_id", "checking"),
                        principal=_money(spec["mortgage"]["principal"], quantum),
                        annual_interest_rate=spec["mortgage"]["annual_interest_rate_ppb"] / 1_000_000_000,
                        term_months=spec["mortgage"]["term_months"],
                    )
                    if spec.get("mortgage") is not None
                    else None
                ),
            )
            for spec in scenario_spec.get("scheduled_property_purchases", [])
        ],
        initial_primary_residences=[
            PrimaryResidenceAssignment(agent_id=spec["agent_id"], property_id=spec["property_id"])
            for spec in scenario_spec.get("initial_primary_residences", [])
        ],
        primary_residence_events=[
            SetPrimaryResidenceEvent(
                month=spec["month"], agent_id=spec["agent_id"], property_id=spec.get("property_id")
            )
            for spec in scenario_spec.get("primary_residence_events", [])
        ],
        property_lifecycle_events=[
            *[
                SetRentedFractionEvent(
                    month=spec["month"],
                    property_id=spec["property_id"],
                    rented_fraction=spec["rented_fraction_ppb"] / 1_000_000_000,
                )
                for spec in scenario_spec.get("property_rented_fraction_events", [])
            ],
            *[
                CapitalImprovementEvent(
                    month=spec["month"],
                    property_id=spec["property_id"],
                    amount=_money(spec["amount"], quantum),
                    description=spec.get("description", ""),
                )
                for spec in scenario_spec.get("capital_improvement_events", [])
            ],
            *[
                PropertySaleEvent(
                    month=spec["month"],
                    property_id=spec["property_id"],
                    closing_cost_pct=spec["closing_cost_bps"] / 100,
                )
                for spec in scenario_spec.get("property_sales", [])
            ],
        ],
        mortgage_interest_deduction_policies=[
            MortgageInterestDeductionPolicy(
                liability_id=spec["liability_id"],
                owner_agent_id=spec["owner_agent_id"],
                debt_class=spec.get("debt_class", "acquisition"),
                per_jurisdiction_principal_cap={
                    jurisdiction_id: _money(cap, quantum)
                    for jurisdiction_id, cap in spec.get(
                        "per_jurisdiction_principal_cap",
                        dict.fromkeys(
                            tax_jurisdiction_ids,
                            int(mortgage_principal_by_liability[spec["liability_id"]] / Decimal(quantum)),
                        ),
                    ).items()
                },
            )
            for spec in scenario_spec.get("mortgage_interest_deduction_policies", [])
        ],
        property_tax_policies=[
            PropertyTaxPolicy(
                property_id=spec["property_id"],
                owner_agent_id=spec["owner_agent_id"],
                from_account_id=spec.get("from_account_id", "checking"),
                tax_authority_agent_id=spec["tax_authority_agent_id"],
                tax_authority_account_id=spec.get("tax_authority_account_id", "checking"),
                annual_tax_rate=(
                    spec["annual_tax_rate_ppb"] / 1_000_000_000 if spec.get("annual_tax_rate_ppb") is not None else None
                ),
                start_month=spec.get("start_month", 0),
                end_month=spec.get("end_month"),
            )
            for spec in scenario_spec.get("property_tax_policies", [])
        ],
        federal_salt_deduction_policies=[
            FederalSaltDeductionPolicy(
                profile_id=spec["profile_id"],
                federal_jurisdiction_id=spec.get("federal_jurisdiction_id", "federal_us"),
                cap_schedule=[
                    FederalSaltCapEntry(
                        effective_year_index=entry["effective_year_index"], cap=_money(entry["cap"], quantum)
                    )
                    for entry in spec.get("cap_schedule", [])
                ],
            )
            for spec in scenario_spec.get("federal_salt_deduction_policies", [])
        ],
        tax_profiles=[
            TaxProfile(
                agent_id=spec["agent_id"],
                jurisdiction_ids=[rules["jurisdiction_id"] for rules in spec["jurisdictions"]],
                tax_authority_agent_id=spec["tax_authority_agent_id"],
                payment_account_id=spec.get("payment_account_id", "checking"),
                tax_authority_account_id=spec.get("tax_authority_account_id", "checking"),
                prior_year_tax=_money(spec.get("prior_year_tax", 0), quantum),
            )
            for spec in scenario_spec.get("tax_profiles", [])
        ],
        horizon_months=scenario_spec["horizon_months"],
    )
    pe_parts = []
    required_pe_channels = {
        "mark",
        "regime",
        "event_kind",
        "sale_opportunity",
        "sale_capacity",
        "eligible",
        "forced_sale",
        "liquidity_blocked",
        "forced_recovery",
        "company_valuation",
    }
    for issuer_id, channels in sorted(pe_channels.items()):
        missing = required_pe_channels - channels.keys()
        if missing:
            raise ValueError(f"private-equity issuer {issuer_id!r} is missing channels {sorted(missing)!r}")
        pe_parts.append(
            PrivateEquityBundle.from_issuer_arrays(
                issuer_id,
                mark_usd_per_unit=channels["mark"].astype(np.float64) * float(Decimal(quantum)),
                regime_code=channels["regime"].astype(np.int64),
                event_kind_code=channels["event_kind"].astype(np.int64),
                sale_opportunity_active=channels["sale_opportunity"].astype(np.bool_),
                sale_capacity_fraction=channels["sale_capacity"].astype(np.float64) / 1_000_000_000,
                eligible_fraction=channels["eligible"].astype(np.float64) / 1_000_000_000,
                forced_sale_fraction=channels["forced_sale"].astype(np.float64) / 1_000_000_000,
                liquidity_blocked=channels["liquidity_blocked"].astype(np.bool_),
                forced_recovery_cashout_usd=channels["forced_recovery"].astype(np.float64) * float(Decimal(quantum)),
                company_valuation_usd=channels["company_valuation"].astype(np.float64) * float(Decimal(quantum)),
                rollout_count=rollout_count,
                horizon_months=scenario_spec["horizon_months"],
            )
        )
    external = ExternalSeriesContext.from_level_blocks(
        level_blocks,
        rollout_count=rollout_count,
        horizon_months=scenario_spec["horizon_months"],
        private_equity=PrivateEquityBundle.combine(pe_parts),
    )
    locations = {
        spec["location_id"]: Location(
            location_id=spec["location_id"],
            display_name=spec["display_name"],
            jurisdiction_ids=spec.get("jurisdiction_ids", []),
            annual_property_tax_rate=spec["annual_property_tax_rate_ppb"] / 1_000_000_000,
            annual_special_assessment=_money(spec.get("annual_special_assessment", 0), quantum),
        )
        for spec in scenario_spec.get("locations", [])
    }
    return scenario, external, locations


def run_legacy_fixture(fixture: dict[str, Any]):
    scenario, external, locations = build_legacy_fixture(fixture)
    return simulate_with_external_series(
        scenario, rollout_count=cast(int, fixture["rollout_count"]), external_series=external, locations=locations
    )
