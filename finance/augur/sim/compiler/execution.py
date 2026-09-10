"""Prepare the execution input directly from an authored scenario and materialized paths.

Resolve tax rules and quantize money, quantities and index levels once. The resulting
typed value is what the engine executes, not an adapter over a second compiled world model.
Unsupported inputs are rejected rather than silently omitted.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Mapping, Sequence
from decimal import Decimal

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
)
from finance.augur.sim.bonds import coupon_amount_quanta
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.private_equity import PEChannels, compile_pe_channels
from finance.augur.sim.compiler.series import (
    collect_level_series_keys,
    external_series_cubes,
    materialize_level_rows,
    validate_series_indexed_amounts,
)
from finance.augur.sim.compiler.tax import compile_tax
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
    round_ppb,
    sampled_array_to_quanta,
)
from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedAccount,
    PreparedAmount,
    PreparedBond,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedFixedAmount,
    PreparedHoldingPool,
    PreparedIndexedAmount,
    PreparedIndexedCoupon,
    PreparedJurisdiction,
    PreparedLocation,
    PreparedLot,
    PreparedObligation,
    PreparedPropertyCashflow,
    PreparedRecurringObligation,
    PreparedRecurringPropertyCashflow,
    PreparedRecurringTransfer,
    PreparedScenario,
    PreparedSeries,
    PreparedTlhPortfolio,
    PreparedTransfer,
    _AllocationPolicy,
    _CapitalImprovement,
    _MortgageFinancing,
    _MortgageInterestDeduction,
    _PrimaryResidence,
    _PrimaryResidenceEvent,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
    _RentedFraction,
    _SaltCap,
    _SaltDeduction,
    _ScheduledSale,
    _SleeveTarget,
    _TenderPolicy,
)
from finance.augur.sim.scenario import (
    CapitalImprovementEvent,
    DriftBand,
    FixedAmount,
    InitialLot,
    PropertySaleEvent,
    Scenario,
    SeriesIndexedAmount,
    SetRentedFractionEvent,
)

_BASIS_POINT_SCALE = 10_000
_MONEY_SERIES_KINDS = (SecurityKey, SecurityDistributionKey, HomeValueKey)
_INDEX_SERIES_KINDS = (InflationKey, RentKey)


class UnsupportedScenarioError(ValueError):
    """A scenario the Rust engine has no representation for.

    Raised rather than encoded: the execution input schema is `deny_unknown_fields`, so a feature with
    no field would have to be dropped, and dropping one changes the answer without changing the
    shape of it.
    """


def _asset_id(asset: AssetKey) -> str:
    """The execution input's flat asset identifier: a bare symbol, or the private-equity wire id."""

    return asset.wire_id if isinstance(asset, PrivateEquityAssetKey) else str(asset.symbol)


def _amount(amount: object, *, quantum: Decimal, context: str) -> PreparedAmount:
    """Resolve exact money once, retaining indexed claims' declared reset convention."""

    match amount:
        case Decimal():
            return int(currency_amount_to_quanta(amount, quantum=quantum))
        case FixedAmount():
            return PreparedFixedAmount(amount=int(currency_amount_to_quanta(amount.amount, quantum=quantum)))
        case SeriesIndexedAmount():
            if not isinstance(amount.series, _INDEX_SERIES_KINDS):
                raise UnsupportedScenarioError(
                    f"{context} is indexed by {amount.series.wire_id!r}, which the execution input's amount "
                    "schedule does not carry; only inflation and rent levels are index series"
                )
            return PreparedIndexedAmount(
                base_amount=int(currency_amount_to_quanta(amount.base_amount, quantum=quantum)),
                series_id=amount.series.wire_id,
                base_month_index=int(amount.base_month_index),
                adjustment_period_months=int(amount.adjustment_period_months),
            )
    raise UnsupportedScenarioError(f"{context} carries an unsupported amount {amount!r}")


def _series_values(
    key: LevelSeriesKey, levels: Float64[np.ndarray, " rollout snapshot"], money: Int64[np.ndarray, " rollout snapshot"]
) -> Int64[np.ndarray, " rollout snapshot"]:
    if not np.isfinite(levels).all():
        rollout, month = np.argwhere(~np.isfinite(levels))[0]
        raise ValueError(
            f"series {key.wire_id!r} has no finite level at rollout {rollout}, month {month}; "
            "the execution input's series are dense over every rollout and snapshot"
        )
    if isinstance(key, SecurityDistributionKey) and np.any(levels < 0):
        rollout, month = np.argwhere(levels < 0)[0]
        raise ValueError(
            f"distribution series {key.wire_id!r} has a negative payout at rollout {rollout}, month {month}"
        )
    if isinstance(key, _MONEY_SERIES_KINDS):
        return money
    if isinstance(key, _INDEX_SERIES_KINDS):
        return round_ppb(levels)
    raise UnsupportedScenarioError(f"level series {key.wire_id!r} has no execution input representation")


def _level_series(
    keys: tuple[LevelSeriesKey, ...],
    levels: Float64[np.ndarray, " series rollout snapshot"],
    money: Int64[np.ndarray, " series rollout snapshot"],
) -> tuple[PreparedSeries, ...]:
    return tuple(
        PreparedSeries(
            series_id=key.wire_id,
            snapshots=levels.shape[2],
            values=tuple(int(value) for value in _series_values(key, levels[row], money[row]).reshape(-1)),
        )
        for row, key in enumerate(keys)
    )


def _private_equity_series(
    issuer_ids: tuple[str, ...],
    pe_channels: PEChannels,
    bundle: PrivateEquityBundle,
    *,
    rollout_count: int,
    horizon_months: int,
    quantum: Decimal,
) -> tuple[PreparedSeries, ...]:
    """The ten per-issuer private-equity channels, in the execution input's typed integer units.

    Execution channels have already passed raw-value validation and money quantization.
    Company valuation uses the same money boundary; it is required by input validation.
    """

    channels = pe_channels.execution
    snapshots = horizon_months + 1
    series = []
    for index, issuer_id in enumerate(issuer_ids):
        valuation = bundle.issuer_float_matrix(
            issuer_id, "company_valuation_usd", rollout_count=rollout_count, horizon_months=horizon_months
        )
        for channel, values in (
            ("mark", channels.mark_quanta[index]),
            ("regime", channels.regime_codes[index]),
            ("event_kind", pe_channels.event_kind_codes[index]),
            ("sale_opportunity", channels.sale_opportunity_active[index].astype(np.int64)),
            ("sale_capacity", round_ppb(channels.sale_capacity_fractions[index])),
            ("eligible", round_ppb(channels.eligible_fractions[index])),
            ("forced_sale", round_ppb(channels.forced_sale_fractions[index])),
            ("liquidity_blocked", channels.liquidity_blocked[index].astype(np.int64)),
            ("forced_recovery", channels.forced_recovery_cashout_quanta[index]),
            ("company_valuation", sampled_array_to_quanta(valuation, quantum=quantum)),
        ):
            series.append(
                PreparedSeries(
                    series_id=f"private_equity_{channel}:{issuer_id}",
                    snapshots=snapshots,
                    values=tuple(int(value) for value in np.asarray(values, dtype=np.int64).reshape(-1)),
                )
            )
    return tuple(series)


def _jurisdiction_identities(
    scenario: Scenario, jurisdictions: Mapping[str, Jurisdiction]
) -> tuple[PreparedJurisdiction, ...]:
    """Every jurisdiction whose LEVEL an interest-exemption rule can name.

    The compiler resolves an issuer's level with `load_jurisdiction` whether or not a tax profile
    names it (`compile_tax`), so a Treasury coupon is state-exempt for a holder who files only in
    California. The registry mirrors that: the profiles' own jurisdictions, plus every issuer a
    bond or fund distribution names.
    """

    levels = {jurisdiction_id: jurisdiction.level for jurisdiction_id, jurisdiction in jurisdictions.items()}
    issuers = {bond.issuer_jurisdiction_id for bond in scenario.initial_bonds} | {
        tax_slice.issuer_jurisdiction_id
        for distribution in scenario.security_distributions
        for tax_slice in distribution.tax_character
    }
    for issuer_id in issuers:
        if issuer_id is not None and issuer_id not in levels:
            levels[issuer_id] = load_jurisdiction(issuer_id).level
    return tuple(
        PreparedJurisdiction(jurisdiction_id=jurisdiction_id, level=levels[jurisdiction_id])
        for jurisdiction_id in sorted(levels)
    )


def _holding_pools(scenario: Scenario) -> tuple[PreparedHoldingPool, ...]:
    pools: dict[tuple[str, str, str], PreparedHoldingPool] = {}
    managed = {(p.owner_agent_id, p.account_id, _asset_id(p.asset)) for p in scenario.tlh_portfolios}

    def add(agent_id: str, account_id: str, asset: AssetKey) -> None:
        asset_id = _asset_id(asset)
        if (agent_id, account_id, asset_id) in managed:
            return
        pools[agent_id, account_id, asset_id] = PreparedHoldingPool(
            agent_id=agent_id, account_id=account_id, asset_id=asset_id, quantity_scale=quantity_scale_for_asset(asset)
        )

    for pool in scenario.holding_pools:
        add(pool.agent_id, pool.account_id, pool.asset)
    for lot in scenario.initial_lots:
        add(lot.agent_id, lot.account_id, lot.asset)
    for policy in scenario.target_allocation_policies:
        account_id = policy.source_account_ids[0] if policy.source_account_ids else policy.account_id
        for sleeve in policy.sleeves:
            add(policy.agent_id, account_id, sleeve.asset)
    return tuple(pools.values())


def _initial_lots(initial_lots: Sequence[InitialLot], *, quantum: Decimal) -> tuple[PreparedLot, ...]:
    lots = []
    for lot in initial_lots:
        scale = quantity_scale_for_asset(lot.asset)
        units = int(quantity_to_quanta(lot.quantity, scale=scale))
        lots.append(
            PreparedLot(
                lot_id=lot.lot_id,
                agent_id=lot.agent_id,
                account_id=lot.account_id,
                asset_id=_asset_id(lot.asset),
                purchase_month=int(lot.purchase_month_index),
                quantity_scale=scale,
                units=units,
                basis=int(currency_amount_to_quanta(lot.cost_basis, quantum=quantum)),
            )
        )
    return tuple(lots)


def _initial_bonds(scenario: Scenario, *, quantum: Decimal) -> tuple[PreparedBond, ...]:
    bonds = []
    for bond in scenario.initial_bonds:
        rate_ppb = rate_to_ppb(bond.annual_coupon_rate)
        face = int(currency_amount_to_quanta(bond.face_value, quantum=quantum))
        coupon = (
            PreparedIndexedCoupon(annual_rate_ppb=rate_ppb)
            if bond.inflation_indexed
            else PreparedFixedAmount(
                amount=coupon_amount_quanta(
                    face_quanta=face,
                    annual_coupon_rate_ppb=rate_ppb,
                    coupon_period_months=int(bond.coupon_period_months),
                )
            )
        )
        bonds.append(
            PreparedBond(
                bond_id=bond.bond_id,
                agent_id=bond.agent_id,
                account_id=bond.account_id,
                issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
                face_value=face,
                purchase_price=int(currency_amount_to_quanta(bond.purchase_price, quantum=quantum)),
                coupon=coupon,
                coupon_period_months=int(bond.coupon_period_months),
                purchase_month_index=int(bond.purchase_month_index),
                maturity_month_index=int(bond.maturity_month_index),
            )
        )
    return tuple(bonds)


def _target_allocation_policies(scenario: Scenario, *, quantum: Decimal) -> tuple[_AllocationPolicy, ...]:
    return tuple(
        _AllocationPolicy(
            agent_id=policy.agent_id,
            account_id=policy.account_id,
            source_account_ids=tuple(policy.source_account_ids),
            sleeves=tuple(
                _SleeveTarget(
                    asset_id=_asset_id(sleeve.asset),
                    weight=int(sleeve.weight),
                    quantity_scale=quantity_scale_for_asset(sleeve.asset),
                )
                for sleeve in policy.sleeves
            ),
            cash_floor=_amount(
                policy.cash_floor, quantum=quantum, context=f"target-allocation floor for {policy.agent_id!r}"
            ),
            cash_ceiling=_amount(
                policy.cash_ceiling, quantum=quantum, context=f"target-allocation ceiling for {policy.agent_id!r}"
            ),
            cause_id_prefix=policy.cause_id_prefix,
            allow_purchases=policy.allow_purchases,
            rebalance_tolerance_ppb=(
                rate_to_ppb(policy.rebalancing.tolerance) if isinstance(policy.rebalancing, DriftBand) else None
            ),
        )
        for policy in scenario.target_allocation_policies
    )


def _property_purchases(scenario: Scenario, *, quantum: Decimal) -> tuple[_PropertyPurchase, ...]:
    return tuple(
        _PropertyPurchase(
            month=int(purchase.month),
            cause_id=purchase.cause_id,
            property_id=purchase.property_id,
            location_id=purchase.location_id,
            buyer_agent_id=purchase.buyer_agent_id,
            buyer_account_id=purchase.buyer_account_id,
            seller_agent_id=purchase.seller_agent_id,
            seller_account_id=purchase.seller_account_id,
            purchase_price=int(currency_amount_to_quanta(purchase.purchase_price, quantum=quantum)),
            down_payment=int(currency_amount_to_quanta(purchase.down_payment, quantum=quantum)),
            buyer_closing_cost=int(currency_amount_to_quanta(purchase.buyer_closing_cost, quantum=quantum)),
            rented_fraction_ppb=rate_to_ppb(purchase.rented_fraction),
            land_value_fraction_ppb=rate_to_ppb(purchase.land_value_fraction),
            mortgage=(
                None
                if purchase.mortgage is None
                else _MortgageFinancing(
                    liability_id=purchase.mortgage.liability_id,
                    lender_agent_id=purchase.mortgage.lender_agent_id,
                    lender_account_id=purchase.mortgage.lender_account_id,
                    principal=int(currency_amount_to_quanta(purchase.mortgage.principal, quantum=quantum)),
                    annual_interest_rate_ppb=rate_to_ppb(purchase.mortgage.annual_interest_rate),
                    term_months=int(purchase.mortgage.term_months),
                )
            ),
        )
        for purchase in scenario.scheduled_property_purchases
    )


def _closing_cost_ppb(event: PropertySaleEvent) -> int:
    """Seller closing costs, on the same grid as every other rate the execution input carries.

    The scenario authors a percent, so the fraction is `pct / 100`. This used to cross in
    basis points, which refused any percent that was not a whole number of them -- 6.375%
    among them -- for no reason but the coarser grid.
    """

    return rate_to_ppb(float(Decimal(str(event.closing_cost_pct)) / 100))


def _locations(
    scenario: Scenario, locations: Mapping[str, Location], *, quantum: Decimal
) -> tuple[PreparedLocation, ...]:
    """The locations this scenario actually buys a property in.

    The rest of the deployment's catalog is places no property is ever bought, and the execution input's
    location list exists for the property-tax policy to read.
    """

    for purchase in scenario.scheduled_property_purchases:
        if purchase.location_id not in locations:
            known_location_ids = ", ".join(repr(location_id) for location_id in sorted(locations)) or "<none>"
            raise ValueError(
                f"scheduled property purchase {purchase.cause_id!r} references unknown location_id "
                f"{purchase.location_id!r}; known location ids: {known_location_ids}"
            )
    referenced = sorted({purchase.location_id for purchase in scenario.scheduled_property_purchases})
    return tuple(
        PreparedLocation(
            location_id=location_id,
            display_name=locations[location_id].display_name,
            jurisdiction_ids=tuple(locations[location_id].jurisdiction_ids),
            annual_property_tax_rate_ppb=rate_to_ppb(locations[location_id].annual_property_tax_rate),
            annual_special_assessment=int(
                currency_amount_to_quanta(locations[location_id].annual_special_assessment, quantum=quantum)
            ),
        )
        for location_id in referenced
    )


def prepare_run(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    jurisdictions: Mapping[str, Jurisdiction],
    locations: Mapping[str, Location],
) -> CompiledRun:
    """Resolve one self-contained execution input; retain no source objects to reread."""
    if rollout_count <= 0:
        raise ValueError(f"rollout_count must be positive; got {rollout_count}")
    quantum = scenario.currency.quantum
    horizon = int(scenario.horizon_months)
    rows = materialize_level_rows(
        tuple(external_series.levels.value_rows()), rollout_count=rollout_count, horizon_months=horizon
    )
    keys = collect_level_series_keys(scenario, rows)
    levels, money = external_series_cubes(
        rows,
        series_index_by_id={key: index for index, key in enumerate(keys)},
        rollout_count=rollout_count,
        horizon_months=horizon,
        currency_quantum=quantum,
    )
    validate_series_indexed_amounts(scenario, rollout_count=rollout_count, rows_by_key={row.key: row for row in rows})
    tax = compile_tax(scenario, jurisdictions)
    issuer_ids = tuple(
        sorted(
            {str(lot.asset.issuer_id) for lot in scenario.initial_lots if isinstance(lot.asset, PrivateEquityAssetKey)}
        )
    )
    pe_channels = compile_pe_channels(
        issuer_ids,
        private_equity=external_series.private_equity,
        rollout_count=rollout_count,
        horizon_months=horizon,
        currency_quantum=quantum,
    )
    lifecycle = scenario.property_lifecycle_events
    return CompiledRun(
        currency_code=scenario.currency.code,
        currency_quantum=format(quantum, "f"),
        rollout_count=rollout_count,
        scenario=PreparedScenario(
            horizon_months=horizon,
            jurisdictions=_jurisdiction_identities(scenario, jurisdictions),
            locations=_locations(scenario, locations, quantum=quantum),
            accounts=tuple(
                PreparedAccount(
                    account=AccountRef(agent_id=balance.agent_id, account_id=balance.account_id),
                    opening_balance=int(currency_amount_to_quanta(balance.balance, quantum=quantum)),
                )
                for balance in scenario.initial_cash
            ),
            holding_pools=_holding_pools(scenario),
            scheduled_transfers=tuple(
                PreparedTransfer(
                    month=int(transfer.month),
                    cause_id=transfer.cause_id,
                    from_account=AccountRef(agent_id=transfer.from_agent_id, account_id=transfer.from_account_id),
                    to_account=AccountRef(agent_id=transfer.to_agent_id, account_id=transfer.to_account_id),
                    amount=_amount(
                        transfer.amount, quantum=quantum, context=f"scheduled transfer {transfer.cause_id!r}"
                    ),
                    income_category=transfer.income_category,
                    deduction_category=transfer.deduction_category,
                )
                for transfer in scenario.scheduled_transfers
            ),
            recurring_transfers=tuple(
                PreparedRecurringTransfer(
                    start_month=int(transfer.start_month),
                    end_month=None if transfer.end_month is None else int(transfer.end_month),
                    cause_id=transfer.cause_id,
                    from_account=AccountRef(agent_id=transfer.from_agent_id, account_id=transfer.from_account_id),
                    to_account=AccountRef(agent_id=transfer.to_agent_id, account_id=transfer.to_account_id),
                    amount=_amount(
                        transfer.amount, quantum=quantum, context=f"recurring transfer {transfer.cause_id!r}"
                    ),
                    income_category=transfer.income_category,
                    deduction_category=transfer.deduction_category,
                )
                for transfer in scenario.recurring_transfers
            ),
            scheduled_property_cashflows=tuple(
                PreparedPropertyCashflow(
                    month=int(cashflow.month),
                    property_id=cashflow.property_id,
                    cause_id=cashflow.cause_id,
                    from_account=AccountRef(agent_id=cashflow.from_agent_id, account_id=cashflow.from_account_id),
                    to_account=AccountRef(agent_id=cashflow.to_agent_id, account_id=cashflow.to_account_id),
                    amount=_amount(
                        cashflow.amount, quantum=quantum, context=f"scheduled property cashflow {cashflow.cause_id!r}"
                    ),
                    income_category=cashflow.income_category,
                    deduction_category=cashflow.deduction_category,
                )
                for cashflow in scenario.scheduled_property_cashflows
            ),
            recurring_property_cashflows=tuple(
                PreparedRecurringPropertyCashflow(
                    start_month=int(cashflow.start_month),
                    end_month=None if cashflow.end_month is None else int(cashflow.end_month),
                    property_id=cashflow.property_id,
                    cause_id=cashflow.cause_id,
                    from_account=AccountRef(agent_id=cashflow.from_agent_id, account_id=cashflow.from_account_id),
                    to_account=AccountRef(agent_id=cashflow.to_agent_id, account_id=cashflow.to_account_id),
                    amount=_amount(
                        cashflow.amount, quantum=quantum, context=f"recurring property cashflow {cashflow.cause_id!r}"
                    ),
                    income_category=cashflow.income_category,
                    deduction_category=cashflow.deduction_category,
                )
                for cashflow in scenario.recurring_property_cashflows
            ),
            obligations=tuple(
                PreparedObligation(
                    month=int(obligation.month),
                    obligation_id=obligation.obligation_id,
                    obligation_type=obligation.obligation_type,
                    from_account=AccountRef(agent_id=obligation.agent_id, account_id=obligation.from_account_id),
                    to_account=AccountRef(agent_id=obligation.to_agent_id, account_id=obligation.to_account_id),
                    amount_due=_amount(
                        obligation.amount_due, quantum=quantum, context=f"obligation {obligation.obligation_id!r}"
                    ),
                    property_id=obligation.property_id,
                    deduction_category=obligation.deduction_category,
                    deductible_fraction_ppb=rate_to_ppb(obligation.deductible_fraction),
                )
                for obligation in scenario.scheduled_obligations
            ),
            recurring_obligations=tuple(
                PreparedRecurringObligation(
                    start_month=int(obligation.start_month),
                    end_month=None if obligation.end_month is None else int(obligation.end_month),
                    obligation_id=obligation.obligation_id,
                    obligation_type=obligation.obligation_type,
                    from_account=AccountRef(agent_id=obligation.agent_id, account_id=obligation.from_account_id),
                    to_account=AccountRef(agent_id=obligation.to_agent_id, account_id=obligation.to_account_id),
                    amount_due=_amount(
                        obligation.amount_due, quantum=quantum, context=f"obligation {obligation.obligation_id!r}"
                    ),
                    property_id=obligation.property_id,
                    deduction_category=obligation.deduction_category,
                    deductible_fraction_ppb=rate_to_ppb(obligation.deductible_fraction),
                )
                for obligation in scenario.recurring_obligations
            ),
            initial_lots=_initial_lots(scenario.initial_lots, quantum=quantum),
            initial_bonds=_initial_bonds(scenario, quantum=quantum),
            _scheduled_sales=tuple(
                _ScheduledSale(
                    month=int(sale.month),
                    cause_id=sale.cause_id,
                    agent_id=sale.agent_id,
                    account_id=sale.source_account_id,
                    asset_id=_asset_id(sale.asset),
                    units=int(quantity_to_quanta(sale.quantity, scale=quantity_scale_for_asset(sale.asset))),
                    proceeds_account_id=sale.proceeds_account_id,
                )
                for sale in scenario.scheduled_asset_sales
            ),
            tax_profiles=tax.profiles,
            income_sources=tax.income_sources,
            distributions=tuple(
                PreparedDistribution(
                    agent_id=distribution.agent_id,
                    holding_account_id=distribution.holding_account_id,
                    asset_id=_asset_id(distribution.asset),
                    to_account_id=distribution.to_account_id,
                    tax_character=tuple(
                        PreparedDistributionSlice(
                            fraction_ppb=rate_to_ppb(tax_slice.fraction),
                            issuer_jurisdiction_id=tax_slice.issuer_jurisdiction_id,
                        )
                        for tax_slice in distribution.tax_character
                    ),
                )
                for distribution in scenario.security_distributions
            ),
            _target_allocation_policies=_target_allocation_policies(scenario, quantum=quantum),
            _private_equity_tender_policies=tuple(
                _TenderPolicy(
                    owner_agent_id=policy.owner_agent_id,
                    proceeds_account_id=policy.proceeds_account_id,
                    liquid_net_worth_floor=_amount(
                        policy.liquid_net_worth_floor,
                        quantum=quantum,
                        context=f"private-equity floor for {policy.owner_agent_id!r}",
                    ),
                )
                for policy in scenario.private_equity_tender_policies
            ),
            tlh_portfolios=tuple(
                PreparedTlhPortfolio(
                    portfolio_id=portfolio.portfolio_id,
                    owner_agent_id=portfolio.owner_agent_id,
                    account_id=portfolio.account_id,
                    asset_id=_asset_id(portfolio.asset),
                    quantity_scale=quantity_scale_for_asset(portfolio.asset),
                    initial_cohorts=_initial_lots(portfolio.initial_lots, quantum=quantum),
                    assumptions=portfolio.assumptions,
                )
                for portfolio in scenario.tlh_portfolios
            ),
            _scheduled_property_purchases=_property_purchases(scenario, quantum=quantum),
            _initial_primary_residences=tuple(
                _PrimaryResidence(agent_id=assignment.agent_id, property_id=assignment.property_id)
                for assignment in scenario.initial_primary_residences
            ),
            _primary_residence_events=tuple(
                _PrimaryResidenceEvent(month=int(event.month), agent_id=event.agent_id, property_id=event.property_id)
                for event in scenario.primary_residence_events
            ),
            _property_rented_fraction_events=tuple(
                _RentedFraction(
                    month=int(event.month),
                    property_id=event.property_id,
                    rented_fraction_ppb=rate_to_ppb(event.rented_fraction),
                )
                for event in lifecycle
                if isinstance(event, SetRentedFractionEvent)
            ),
            _capital_improvement_events=tuple(
                _CapitalImprovement(
                    month=int(event.month),
                    property_id=event.property_id,
                    amount=int(currency_amount_to_quanta(event.amount, quantum=quantum)),
                    description=event.description,
                )
                for event in lifecycle
                if isinstance(event, CapitalImprovementEvent)
            ),
            _property_sales=tuple(
                _PropertySale(
                    month=int(event.month), property_id=event.property_id, closing_cost_ppb=_closing_cost_ppb(event)
                )
                for event in lifecycle
                if isinstance(event, PropertySaleEvent)
            ),
            _mortgage_interest_deduction_policies=tuple(
                _MortgageInterestDeduction(
                    liability_id=policy.liability_id,
                    owner_agent_id=policy.owner_agent_id,
                    debt_class=policy.debt_class,
                    per_jurisdiction_principal_cap={
                        jurisdiction_id: int(currency_amount_to_quanta(cap, quantum=quantum))
                        for jurisdiction_id, cap in policy.per_jurisdiction_principal_cap.items()
                    },
                )
                for policy in scenario.mortgage_interest_deduction_policies
            ),
            _property_tax_policies=tuple(
                _PropertyTax(
                    property_id=policy.property_id,
                    owner_agent_id=policy.owner_agent_id,
                    from_account_id=policy.from_account_id,
                    tax_authority_agent_id=policy.tax_authority_agent_id,
                    tax_authority_account_id=policy.tax_authority_account_id,
                    annual_tax_rate_ppb=None if policy.annual_tax_rate is None else rate_to_ppb(policy.annual_tax_rate),
                    start_month=int(policy.start_month),
                    end_month=None if policy.end_month is None else int(policy.end_month),
                )
                for policy in scenario.property_tax_policies
            ),
            _federal_salt_deduction_policies=tuple(
                _SaltDeduction(
                    profile_id=policy.profile_id,
                    federal_jurisdiction_id=policy.federal_jurisdiction_id,
                    cap_schedule=tuple(
                        _SaltCap(
                            effective_year_index=int(entry.effective_year_index),
                            cap=int(currency_amount_to_quanta(entry.cap, quantum=quantum)),
                        )
                        for entry in policy.cap_schedule
                    ),
                )
                for policy in scenario.federal_salt_deduction_policies
            ),
        ),
        series=(
            *_level_series(keys, levels, money),
            *_private_equity_series(
                issuer_ids,
                pe_channels,
                external_series.private_equity,
                rollout_count=rollout_count,
                horizon_months=horizon,
                quantum=quantum,
            ),
        ),
    )
