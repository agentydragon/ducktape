"""Lower authored declarations and sampled paths into the prepared records a composed world declares.

Quantize money, quantities and index levels once, per table. Unsupported inputs are
rejected rather than silently omitted.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
)
from finance.augur.sim.bonds import coupon_amount_quanta
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.private_equity import compile_pe_channels
from finance.augur.sim.compiler.series import external_series_cubes, materialize_level_rows
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    rate_to_ppb,
    round_ppb,
    sampled_array_to_quanta,
)
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId
from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.prepared import (
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
    PreparedPropertyCashflow,
    PreparedRecurringObligation,
    PreparedRecurringPropertyCashflow,
    PreparedSeries,
    PreparedTlhPortfolio,
    _CapitalImprovement,
    _MortgageFinancing,
    _MortgageInterestDeduction,
    _PrimaryResidence,
    _PrimaryResidenceEvent,
    _PropertyPurchase,
    _PropertySale,
    _PropertyTax,
    _RentedFraction,
    _TenderPolicy,
)
from finance.augur.sim.property import Housing
from finance.augur.sim.scenario import (
    BondHolding,
    CapitalImprovementEvent,
    FixedAmount,
    InitialAccountBalance,
    InitialLot,
    InterestIncome,
    MortgageInterestDeductionPolicy,
    PrimaryResidenceAssignment,
    PrivateEquityTenderPolicy,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringPropertyCashflow,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    TlhPortfolioSpec,
)
from finance.augur.sim.tlh import TlhOpeningCohort

_MONEY_SERIES_KINDS = (SecurityKey, SecurityDistributionKey, HomeValueKey)
_INDEX_SERIES_KINDS = (InflationKey, RentKey)


class UnsupportedScenarioError(ValueError):
    """An authored input the prepared records have no representation for.

    Raised rather than dropped: dropping a feature changes the answer without changing its shape.
    """


def _asset_id(asset: AssetKey) -> AssetId:
    """The execution input's flat asset identifier: a bare symbol, or the private-equity wire id."""

    return AssetId(asset.wire_id if isinstance(asset, PrivateEquityAssetKey) else asset.symbol)


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


def compile_series(
    external_series: ExternalSeriesContext, *, rollout_count: int, horizon_months: int, currency_quantum: Decimal
) -> tuple[PreparedSeries, ...]:
    """The sampled level series as integer paths.

    Only sampled keys are carried; a composed world checks at `declare_pool` that the
    series a pool needs is present.
    """
    rows = materialize_level_rows(
        tuple(external_series.levels.value_rows()), rollout_count=rollout_count, horizon_months=horizon_months
    )
    keys = tuple(row.key for row in rows)
    levels, money = external_series_cubes(
        rows,
        series_index_by_id={key: index for index, key in enumerate(keys)},
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        currency_quantum=currency_quantum,
    )
    return _level_series(keys, levels, money)


def compile_private_equity_series(
    issuer_ids: Sequence[IssuerId],
    bundle: PrivateEquityBundle,
    *,
    rollout_count: int,
    horizon_months: int,
    quantum: Decimal,
) -> tuple[PreparedSeries, ...]:
    """The ten per-issuer private-equity channels, in the execution input's typed integer units.

    `compile_pe_channels` validates raw values and quantizes money; company valuation crosses
    the same money boundary here.
    """

    pe_channels = compile_pe_channels(
        tuple(issuer_ids),
        private_equity=bundle,
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        currency_quantum=quantum,
    )
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


def compile_jurisdictions(
    jurisdictions: Mapping[JurisdictionId, Jurisdiction],
    *,
    bonds: Iterable[BondHolding],
    distributions: Iterable[SecurityDistribution],
) -> tuple[PreparedJurisdiction, ...]:
    """Every jurisdiction whose LEVEL an interest-exemption rule can name.

    The compiler resolves an issuer's level with `load_jurisdiction` whether or not a tax profile
    names it (`compile_income_sources`), so a Treasury coupon is state-exempt for a holder who files only in
    California. The registry mirrors that: the profiles' own `jurisdictions`, plus every issuer a
    bond or fund distribution names.
    """

    levels = {jurisdiction_id: jurisdiction.level for jurisdiction_id, jurisdiction in jurisdictions.items()}
    issuers = {bond.issuer_jurisdiction_id for bond in bonds} | {
        tax_slice.income_category.issuer_jurisdiction_id
        for distribution in distributions
        for tax_slice in distribution.tax_character
        if isinstance(tax_slice.income_category, InterestIncome)
    }
    for issuer_id in issuers:
        if issuer_id is not None and issuer_id not in levels:
            levels[issuer_id] = load_jurisdiction(issuer_id).level
    return tuple(
        PreparedJurisdiction(jurisdiction_id=jurisdiction_id, level=levels[jurisdiction_id])
        for jurisdiction_id in sorted(levels)
    )


def compile_accounts(balances: Iterable[InitialAccountBalance], *, quantum: Decimal) -> tuple[PreparedAccount, ...]:
    return tuple(
        PreparedAccount(
            account=AccountRef(agent_id=balance.agent_id, account_id=balance.account_id),
            opening_balance=int(currency_amount_to_quanta(balance.balance, quantum=quantum)),
        )
        for balance in balances
    )


def compile_holding_pools(*, lots: Iterable[InitialLot]) -> tuple[PreparedHoldingPool, ...]:
    """Every pool a lot names, once.

    A lot's pool on a managed slot stays, so the world refuses the lot beside the portfolio.
    """
    prepared: dict[tuple[AgentId, AccountId, AssetId], PreparedHoldingPool] = {}
    for lot in lots:
        asset_id = _asset_id(lot.asset)
        prepared[lot.agent_id, lot.account_id, asset_id] = PreparedHoldingPool(
            agent_id=lot.agent_id,
            account_id=lot.account_id,
            asset_id=asset_id,
            quantity_scale=quantity_scale_for_asset(lot.asset),
        )
    return tuple(prepared.values())


def compile_lots(lots: Iterable[InitialLot], *, quantum: Decimal) -> tuple[PreparedLot, ...]:
    prepared = []
    for lot in lots:
        scale = quantity_scale_for_asset(lot.asset)
        prepared.append(
            PreparedLot(
                lot_id=lot.lot_id,
                agent_id=lot.agent_id,
                account_id=lot.account_id,
                asset_id=_asset_id(lot.asset),
                purchase_month=int(lot.purchase_month_index),
                quantity_scale=scale,
                units=int(quantity_to_quanta(lot.quantity, scale=scale)),
                basis=int(currency_amount_to_quanta(lot.cost_basis, quantum=quantum)),
            )
        )
    return tuple(prepared)


def compile_bond(bond: BondHolding, *, quantum: Decimal) -> PreparedBond:
    rate_ppb = rate_to_ppb(bond.annual_coupon_rate)
    face = int(currency_amount_to_quanta(bond.face_value, quantum=quantum))
    coupon = (
        PreparedIndexedCoupon(annual_rate_ppb=rate_ppb)
        if bond.inflation_indexed
        else PreparedFixedAmount(
            amount=coupon_amount_quanta(
                face_quanta=face, annual_coupon_rate_ppb=rate_ppb, coupon_period_months=int(bond.coupon_period_months)
            )
        )
    )
    return PreparedBond(
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


def compile_distribution(distribution: SecurityDistribution) -> PreparedDistribution:
    return PreparedDistribution(
        agent_id=distribution.agent_id,
        holding_account_id=distribution.holding_account_id,
        asset_id=_asset_id(distribution.asset),
        to_account_id=distribution.to_account_id,
        tax_character=tuple(
            PreparedDistributionSlice(
                fraction_ppb=rate_to_ppb(tax_slice.fraction), income_category=tax_slice.income_category
            )
            for tax_slice in distribution.tax_character
        ),
    )


def compile_tlh_portfolio(portfolio: TlhPortfolioSpec, *, quantum: Decimal) -> PreparedTlhPortfolio:
    return PreparedTlhPortfolio(
        portfolio_id=portfolio.portfolio_id,
        owner_agent_id=portfolio.owner_agent_id,
        account_id=portfolio.account_id,
        asset_id=_asset_id(portfolio.asset),
        initial_cohorts=tuple(
            TlhOpeningCohort(
                value=int(currency_amount_to_quanta(cohort.value, quantum=quantum)),
                cost_basis=int(currency_amount_to_quanta(cohort.cost_basis, quantum=quantum)),
                purchase_month_index=cohort.purchase_month_index,
            )
            for cohort in portfolio.initial_cohorts
        ),
        assumptions=portfolio.assumptions,
    )


def compile_tender_policy(policy: PrivateEquityTenderPolicy, *, quantum: Decimal) -> _TenderPolicy:
    return _TenderPolicy(
        owner_agent_id=policy.owner_agent_id,
        proceeds_account_id=policy.proceeds_account_id,
        liquid_net_worth_floor=_amount(
            policy.liquid_net_worth_floor,
            quantum=quantum,
            context=f"private-equity floor for {policy.owner_agent_id!r}",
        ),
    )


def compile_property_cashflow(cashflow: ScheduledPropertyCashflow, *, quantum: Decimal) -> PreparedPropertyCashflow:
    return PreparedPropertyCashflow(
        month=int(cashflow.month),
        property_id=cashflow.property_id,
        cause_id=cashflow.cause_id,
        from_account=AccountRef(agent_id=cashflow.from_agent_id, account_id=cashflow.from_account_id),
        to_account=AccountRef(agent_id=cashflow.to_agent_id, account_id=cashflow.to_account_id),
        amount=_amount(cashflow.amount, quantum=quantum, context=f"scheduled property cashflow {cashflow.cause_id!r}"),
        income_category=cashflow.income_category,
        deduction_category=cashflow.deduction_category,
    )


def compile_recurring_property_cashflow(
    cashflow: RecurringPropertyCashflow, *, quantum: Decimal
) -> PreparedRecurringPropertyCashflow:
    return PreparedRecurringPropertyCashflow(
        start_month=int(cashflow.start_month),
        end_month=None if cashflow.end_month is None else int(cashflow.end_month),
        property_id=cashflow.property_id,
        cause_id=cashflow.cause_id,
        from_account=AccountRef(agent_id=cashflow.from_agent_id, account_id=cashflow.from_account_id),
        to_account=AccountRef(agent_id=cashflow.to_agent_id, account_id=cashflow.to_account_id),
        amount=_amount(cashflow.amount, quantum=quantum, context=f"recurring property cashflow {cashflow.cause_id!r}"),
        income_category=cashflow.income_category,
        deduction_category=cashflow.deduction_category,
    )


def compile_recurring_obligation(obligation: RecurringObligation, *, quantum: Decimal) -> PreparedRecurringObligation:
    return PreparedRecurringObligation(
        start_month=int(obligation.start_month),
        end_month=None if obligation.end_month is None else int(obligation.end_month),
        obligation_id=obligation.obligation_id,
        obligation_type=obligation.obligation_type,
        from_account=AccountRef(agent_id=obligation.agent_id, account_id=obligation.from_account_id),
        to_account=AccountRef(agent_id=obligation.to_agent_id, account_id=obligation.to_account_id),
        amount_due=_amount(obligation.amount_due, quantum=quantum, context=f"obligation {obligation.obligation_id!r}"),
        property_id=obligation.property_id,
        deduction_category=obligation.deduction_category,
        deductible_fraction_ppb=rate_to_ppb(obligation.deductible_fraction),
    )


def _closing_cost_ppb(event: PropertySaleEvent) -> int:
    """Seller closing costs, on the same grid as every other rate the execution input carries.

    The scenario authors a percent, so the fraction is `pct / 100`. This used to cross in
    basis points, which refused any percent that was not a whole number of them -- 6.375%
    among them -- for no reason but the coarser grid.
    """

    return rate_to_ppb(float(Decimal(str(event.closing_cost_pct)) / 100))


def compile_housing(
    *,
    purchases: Iterable[ScheduledPropertyPurchase],
    initial_residences: Iterable[PrimaryResidenceAssignment],
    residence_events: Iterable[SetPrimaryResidenceEvent],
    lifecycle_events: Sequence[PropertyLifecycleEvent],
    quantum: Decimal,
) -> Housing:
    """Scripted purchases, their residence assignments and their lifecycle, as the tables `Properties` reads."""
    return Housing(
        purchases=tuple(
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
            for purchase in purchases
        ),
        sales=tuple(
            _PropertySale(
                month=int(event.month), property_id=event.property_id, closing_cost_ppb=_closing_cost_ppb(event)
            )
            for event in lifecycle_events
            if isinstance(event, PropertySaleEvent)
        ),
        initial_residences=tuple(
            _PrimaryResidence(agent_id=assignment.agent_id, property_id=assignment.property_id)
            for assignment in initial_residences
        ),
        residence_events=tuple(
            _PrimaryResidenceEvent(month=int(event.month), agent_id=event.agent_id, property_id=event.property_id)
            for event in residence_events
        ),
        rented_fraction_events=tuple(
            _RentedFraction(
                month=int(event.month),
                property_id=event.property_id,
                rented_fraction_ppb=rate_to_ppb(event.rented_fraction),
            )
            for event in lifecycle_events
            if isinstance(event, SetRentedFractionEvent)
        ),
        capital_improvements=tuple(
            _CapitalImprovement(
                month=int(event.month),
                property_id=event.property_id,
                amount=int(currency_amount_to_quanta(event.amount, quantum=quantum)),
                description=event.description,
            )
            for event in lifecycle_events
            if isinstance(event, CapitalImprovementEvent)
        ),
    )


def compile_locations(
    purchases: Sequence[ScheduledPropertyPurchase], locations: Mapping[LocationId, Location], *, quantum: Decimal
) -> tuple[PreparedLocation, ...]:
    """The locations the purchases buy in.

    The rest of the deployment's catalog is places no property is ever bought, and the execution input's
    location list exists for the property-tax policy to read.
    """

    for purchase in purchases:
        if purchase.location_id not in locations:
            known_location_ids = ", ".join(repr(location_id) for location_id in sorted(locations)) or "<none>"
            raise ValueError(
                f"scheduled property purchase {purchase.cause_id!r} references unknown location_id "
                f"{purchase.location_id!r}; known location ids: {known_location_ids}"
            )
    referenced = sorted({purchase.location_id for purchase in purchases})
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


def compile_property_tax(policy: PropertyTaxPolicy) -> _PropertyTax:
    return _PropertyTax(
        property_id=policy.property_id,
        owner_agent_id=policy.owner_agent_id,
        from_account_id=policy.from_account_id,
        tax_authority_agent_id=policy.tax_authority_agent_id,
        tax_authority_account_id=policy.tax_authority_account_id,
        annual_tax_rate_ppb=None if policy.annual_tax_rate is None else rate_to_ppb(policy.annual_tax_rate),
        start_month=int(policy.start_month),
        end_month=None if policy.end_month is None else int(policy.end_month),
    )


def compile_interest_deduction(
    policy: MortgageInterestDeductionPolicy, *, quantum: Decimal
) -> _MortgageInterestDeduction:
    return _MortgageInterestDeduction(
        liability_id=policy.liability_id,
        owner_agent_id=policy.owner_agent_id,
        debt_class=policy.debt_class,
        per_jurisdiction_principal_cap={
            jurisdiction_id: int(currency_amount_to_quanta(cap, quantum=quantum))
            for jurisdiction_id, cap in policy.per_jurisdiction_principal_cap.items()
        },
    )
