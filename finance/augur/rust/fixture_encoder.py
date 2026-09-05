"""Encode a `Scenario` and its compiled sampled paths as the Rust simulator's integer fixture.

This is the only direction: what `product/service.py` dispatches to the Rust engine, and what
the differential suites encode a case with, so nothing the JAX engine runs reaches Rust by a
second derivation.

Money crosses exactly. `CompiledSimulation.external_money_values` is already the integer
quantum count the JAX engine itself reads, and configured amounts go through the same
`currency_amount_to_quanta` boundary the compiler uses, so neither side rounds twice.

Index levels (inflation, rent) are float64 in the plan and parts per billion in the fixture.
Every JAX site that turns one into money quantizes it to PPB first — `_scale_money_by_float_ratio`
rounds both the numerator and the denominator level with `_round_int64(level * MONEY_FACTOR_SCALE)`
before dividing integers — so pre-quantizing here hands Rust the very integers JAX would have
formed, and the two engines divide the same rational.

A scenario feature the fixture cannot express raises instead of being dropped. The Rust engine
models a subset of the Python one, and a silently discarded feature is how a fan that looks
right is wrong.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
)
from finance.augur.product.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.sim.bonds import MONTHS_PER_YEAR
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.compiler.tax import OPEN_ENDED_BRACKET_UPPER_QUANTA
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    MONEY_FACTOR_SCALE,
    currency_amount_to_quanta,
    quantity_scale_for_asset,
    quantity_to_quanta,
    sampled_array_to_quanta,
)
from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    CapitalImprovementEvent,
    FixedAmount,
    InterestIncome,
    OrdinaryIncome,
    PropertySaleEvent,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    Scenario,
    ScheduledObligation,
    ScheduledPropertyCashflow,
    ScheduledTransfer,
    SeriesIndexedAmount,
    SetRentedFractionEvent,
)

# Mirrors `FIXTURE_SCHEMA_VERSION` in `fixture.rs`; the simulator rejects any other value, so a
# schema bump fails loudly here rather than encoding a document the engine will not read.
FIXTURE_SCHEMA_VERSION = 8

_BASIS_POINT_SCALE = 10_000
_MONEY_SERIES_KINDS = (SecurityKey, SecurityDistributionKey, HomeValueKey)
_INDEX_SERIES_KINDS = (InflationKey, RentKey)


class UnsupportedScenarioError(ValueError):
    """A scenario the Rust engine has no representation for.

    Raised rather than encoded: the fixture schema is `deny_unknown_fields`, so a feature with
    no field would have to be dropped, and dropping one changes the answer without changing the
    shape of it.
    """


def _round_ppb(values: Float64[np.ndarray, " *shape"] | float) -> Int64[np.ndarray, " *shape"]:
    """Quantize a dimensionless level or rate exactly as `jax_engine._round_int64` does.

    Half away from zero on `value * MONEY_FACTOR_SCALE`, which is the rounding every JAX site
    applies to a level or rate before it multiplies integer money.
    """

    scaled = np.asarray(values, dtype=np.float64) * MONEY_FACTOR_SCALE
    return (np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)).astype(np.int64)


def _ppb(value: float) -> int:
    return int(_round_ppb(value))


def _exact_ppb(value: float, *, context: str) -> int:
    """A rate whose decimal spelling is exact in PPB.

    Rust reads the PPB integer where the compiler reads the configured float as an exact
    rational — `Fraction(str(rate))` for a bond coupon, `Decimal(str(rate))` for a mortgage.
    The two are the same number only when the decimal has at most nine places, so a finer rate
    is refused rather than silently rounded on one side.
    """

    scaled = Decimal(str(value)) * MONEY_FACTOR_SCALE
    if scaled != scaled.to_integral_value():
        raise UnsupportedScenarioError(f"{context} {value} is not exactly representable in parts per billion")
    return int(scaled)


def _account(agent_id: str, account_id: str) -> dict[str, str]:
    return {"agent_id": agent_id, "account_id": account_id}


def _asset_id(asset: AssetKey) -> str:
    """The fixture's flat asset identifier: a bare symbol, or the private-equity wire id."""

    return asset.wire_id if isinstance(asset, PrivateEquityAssetKey) else str(asset.symbol)


def _amount(amount: object, *, quantum: Decimal, context: str) -> int | dict[str, Any]:
    """One `AmountSpec` as the fixture's untagged amount: an integer, or a tagged schedule."""

    match amount:
        case Decimal():
            return int(currency_amount_to_quanta(amount, quantum=quantum))
        case FixedAmount():
            return {"kind": "fixed", "amount": int(currency_amount_to_quanta(amount.amount, quantum=quantum))}
        case SeriesIndexedAmount():
            if not isinstance(amount.series, _INDEX_SERIES_KINDS):
                raise UnsupportedScenarioError(
                    f"{context} is indexed by {amount.series.wire_id!r}, which the fixture's amount "
                    "schedule does not carry; only inflation and rent levels are index series"
                )
            return {
                "kind": "series_indexed",
                "base_amount": int(currency_amount_to_quanta(amount.base_amount, quantum=quantum)),
                "series_id": amount.series.wire_id,
                "base_month_index": int(amount.base_month_index),
                "adjustment_period_months": int(amount.adjustment_period_months),
            }
    raise UnsupportedScenarioError(f"{context} carries an unsupported amount {amount!r}")


def _income_category(category: OrdinaryIncome | InterestIncome | None, *, context: str) -> str | None:
    """The fixture tags ordinary income only; interest reaches income through its instrument."""

    match category:
        case None:
            return None
        case OrdinaryIncome():
            return "ordinary"
    raise UnsupportedScenarioError(
        f"{context} tags income as {category!r}; the fixture's transfers carry only ordinary income"
    )


def _span(start_month: int, end_month: int | None) -> dict[str, Any]:
    """The window a recurring spec fires in: `end_month` is inclusive, and absent means the horizon."""

    return {"start_month": int(start_month), "end_month": None if end_month is None else int(end_month)}


def _flow(
    flow: ScheduledTransfer | RecurringTransfer | ScheduledPropertyCashflow | RecurringPropertyCashflow,
    *,
    quantum: Decimal,
    context: str,
) -> dict[str, Any]:
    """What a transfer-shaped spec carries besides its dates and, for a cashflow, its property.

    Four scenario families cross as four fixture structs — transfers and property cashflows,
    each one-shot or recurring — and they differ only in those two things.
    """

    return {
        "cause_id": flow.cause_id,
        "from": _account(flow.from_agent_id, flow.from_account_id),
        "to": _account(flow.to_agent_id, flow.to_account_id),
        "amount": _amount(flow.amount, quantum=quantum, context=context),
        "income_category": _income_category(flow.income_category, context=context),
        "deduction_category": flow.deduction_category,
    }


def _obligation(obligation: ScheduledObligation | RecurringObligation, *, quantum: Decimal) -> dict[str, Any]:
    """What an obligation carries besides its dates."""

    context = f"obligation {obligation.obligation_id!r}"
    return {
        "obligation_id": obligation.obligation_id,
        "obligation_type": obligation.obligation_type,
        "from": _account(obligation.agent_id, obligation.from_account_id),
        "to": _account(obligation.to_agent_id, obligation.to_account_id),
        "amount_due": _amount(obligation.amount_due, quantum=quantum, context=context),
        "property_id": obligation.property_id,
        "deduction_category": obligation.deduction_category,
    }


def _reject_unsupported_obligation(obligation: ScheduledObligation | RecurringObligation) -> None:
    """The obligation shape the fixture's `ObligationSpec` still has nowhere to put.

    `property_id` and `deduction_category` cross as themselves; `deductible_fraction` does not,
    and only has to when no property is named. A property-tied obligation takes its Schedule E
    share from that property's runtime rented fraction on both sides, which leaves the
    compile-time fraction inert. With no property the fixture deducts the whole payment, so a
    partial fraction is refused rather than silently widened to all of it.
    """

    if (
        obligation.deduction_category is not None
        and obligation.property_id is None
        and obligation.deductible_fraction != 1.0
    ):
        raise UnsupportedScenarioError(
            f"obligation {obligation.obligation_id!r} deducts {obligation.deductible_fraction} of what it "
            "pays; the Rust fixture's obligations deduct all of it unless a property supplies the fraction"
        )


def _series_values(key: LevelSeriesKey, plan: CompiledSimulation, row: int) -> Int64[np.ndarray, " rollout snapshot"]:
    if isinstance(key, _MONEY_SERIES_KINDS):
        return np.asarray(plan.external_money_values[row], dtype=np.int64)
    if isinstance(key, _INDEX_SERIES_KINDS):
        levels = plan.external_values[row]
        if not np.isfinite(levels).all():
            rollout, month = np.argwhere(~np.isfinite(levels))[0]
            raise ValueError(
                f"index series {key.wire_id!r} has no level at rollout {rollout}, month {month}; "
                "the fixture's series are dense over every rollout and snapshot"
            )
        return _round_ppb(levels)
    raise UnsupportedScenarioError(f"level series {key.wire_id!r} has no fixture representation")


def _level_series(plan: CompiledSimulation) -> list[dict[str, Any]]:
    snapshots = plan.horizon_months + 1
    return [
        {
            "series_id": key.wire_id,
            "snapshots": snapshots,
            "values": _series_values(key, plan, row).reshape(-1).tolist(),
        }
        for row, key in enumerate(plan.series_keys)
    ]


def _private_equity_series(plan: CompiledSimulation, bundle: PrivateEquityBundle) -> list[dict[str, Any]]:
    """The ten per-issuer private-equity channels, in the fixture's typed integer units.

    Nine come off the compiled channels the JAX engine executes, so both engines read one
    materialization of the sampled bundle. `company_valuation` is the exception: the compiler
    drops it because no engine phase reads it, while the Rust validator still requires the
    channel, so it comes off the bundle at the same money boundary as the marks.
    """

    channels = plan.pe_channels.execution
    snapshots = plan.horizon_months + 1
    series: list[dict[str, Any]] = []
    for index, issuer_id in enumerate(plan.pe_issuers.issuer_ids):
        valuation = bundle.issuer_float_matrix(
            issuer_id, "company_valuation_usd", rollout_count=plan.rollout_count, horizon_months=plan.horizon_months
        )
        for channel, values in (
            ("mark", channels.mark_quanta[index]),
            ("regime", channels.regime_codes[index]),
            ("event_kind", plan.pe_channels.event_kind_codes[index]),
            ("sale_opportunity", channels.sale_opportunity_active[index].astype(np.int64)),
            ("sale_capacity", _round_ppb(channels.sale_capacity_fractions[index])),
            ("eligible", _round_ppb(channels.eligible_fractions[index])),
            ("forced_sale", _round_ppb(channels.forced_sale_fractions[index])),
            ("liquidity_blocked", channels.liquidity_blocked[index].astype(np.int64)),
            ("forced_recovery", channels.forced_recovery_cashout_quanta[index]),
            ("company_valuation", sampled_array_to_quanta(valuation, quantum=plan.currency_quantum)),
        ):
            series.append(
                {
                    "series_id": f"private_equity_{channel}:{issuer_id}",
                    "snapshots": snapshots,
                    "values": np.asarray(values, dtype=np.int64).reshape(-1).tolist(),
                }
            )
    return series


def _jurisdiction_identities(scenario: Scenario, jurisdictions: Mapping[str, Jurisdiction]) -> list[dict[str, Any]]:
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
    return [
        {"jurisdiction_id": jurisdiction_id, "level": str(levels[jurisdiction_id])}
        for jurisdiction_id in sorted(levels)
    ]


def _brackets(
    upper: Int64[np.ndarray, " bracket"], rate: Float64[np.ndarray, " bracket"], count: int
) -> list[dict[str, Any]]:
    return [
        {
            "upper": None if int(upper[index]) == OPEN_ENDED_BRACKET_UPPER_QUANTA else int(upper[index]),
            "rate_ppb": _ppb(float(rate[index])),
        }
        for index in range(count)
    ]


def _tax_profiles(
    scenario: Scenario, plan: CompiledSimulation, jurisdictions: Mapping[str, Jurisdiction]
) -> list[dict[str, Any]]:
    """Tax profiles built from the compiled tables rather than re-read from the jurisdiction YAML.

    `plan.tax` already holds the bracket edges, rates, standard deductions, prior-year tax and
    §121 cap the JAX engine will run, each resolved for its profile's filing status. Taking them
    from there is what makes the two engines assess one schedule instead of two lookups that have
    to agree.
    """

    tax = plan.tax
    rules_by_profile: list[list[dict[str, Any]]] = [[] for _ in scenario.tax_profiles]
    for link, profile_index in enumerate(tax.link_profile.tolist()):
        jurisdiction_id = plan.strings[int(tax.link_jurisdiction[link])]
        jurisdiction = jurisdictions[jurisdiction_id]
        rules_by_profile[profile_index].append(
            {
                "jurisdiction_id": jurisdiction_id,
                "exempt_interest_from_levels": sorted(str(level) for level in jurisdiction.exempt_interest_from_levels),
                "exempts_own_issue": jurisdiction.exempts_own_issue,
                "ordinary_brackets": _brackets(
                    tax.link_ordinary_upper[link], tax.link_ordinary_rate[link], int(tax.link_ordinary_count[link])
                ),
                "long_term_capital_gain_brackets": _brackets(
                    tax.link_ltcg_upper[link], tax.link_ltcg_rate[link], int(tax.link_ltcg_count[link])
                ),
                "standard_deduction": int(tax.link_standard_deduction[link]),
                # The taxpayer's own IRC 1211(b) cap, not this engine's constant. It is a
                # per-profile figure because the JAX netting runs once per taxpayer, and
                # `compile_tax` has already refused a profile whose jurisdictions disagree —
                # so every link of a profile carries the same number, and writing it per
                # jurisdiction here is faithful rather than a flattening.
                "max_capital_loss_ordinary_offset": int(
                    tax.profile_max_capital_loss_ordinary_offset[tax.link_profile[link]]
                ),
                "section_1250_rate_ppb": _ppb(float(tax.link_section_1250_rate[link])),
            }
        )
    return [
        {
            "agent_id": profile.agent_id,
            "tax_authority_agent_id": profile.tax_authority_agent_id,
            "payment_account_id": profile.payment_account_id,
            "tax_authority_account_id": profile.tax_authority_account_id,
            "prior_year_tax": int(tax.profile_prior_year_tax[index]),
            "section_121_exclusion": int(tax.profile_section_121_exclusion[index]),
            "jurisdictions": rules_by_profile[index],
        }
        for index, profile in enumerate(scenario.tax_profiles)
    ]


def _initial_lots(scenario: Scenario, *, quantum: Decimal) -> list[dict[str, Any]]:
    lots: list[dict[str, Any]] = []
    for lot in scenario.initial_lots:
        scale = quantity_scale_for_asset(lot.asset)
        units = int(quantity_to_quanta(lot.quantity, scale=scale))
        basis_per_unit = int(currency_amount_to_quanta(lot.cost_basis_per_unit, quantum=quantum))
        total = basis_per_unit * units
        if total % scale:
            raise UnsupportedScenarioError(
                f"lot {lot.lot_id!r} holds {lot.quantity} units at {lot.cost_basis_per_unit} each, whose total "
                "basis is not a whole number of currency quanta; the fixture stores the total"
            )
        lots.append(
            {
                "lot_id": lot.lot_id,
                "agent_id": lot.agent_id,
                "account_id": lot.account_id,
                "asset_id": _asset_id(lot.asset),
                "purchase_month": int(lot.purchase_month_index),
                "quantity_scale": scale,
                "units": units,
                "basis": total // scale,
            }
        )
    return lots


def _reject_inexact_indexed_period_rate(bond_id: str, rate_ppb: int, rate: float, period_months: int) -> None:
    """A TIPS coupon is the one bond rate the two engines reach by different routes.

    Rust divides the PPB annual rate by twelve in integers; the compiler forms the period rate as
    a float64 (`compile_bonds.period_rate`) and the engine rounds that to PPB. They agree for
    every rate whose period share lands on a PPB boundary, and this refuses the rest rather than
    paying a different coupon on each side.
    """

    exact = (2 * rate_ppb * period_months + MONTHS_PER_YEAR) // (2 * MONTHS_PER_YEAR)
    if exact != _ppb(rate * period_months / MONTHS_PER_YEAR):
        raise UnsupportedScenarioError(
            f"inflation-indexed bond {bond_id!r} has a {period_months}-month period rate that is not the "
            "same integer on both sides of the Python/JAX float64 boundary"
        )


def _initial_bonds(scenario: Scenario, *, quantum: Decimal) -> list[dict[str, Any]]:
    bonds: list[dict[str, Any]] = []
    for bond in scenario.initial_bonds:
        rate_ppb = _exact_ppb(bond.annual_coupon_rate, context=f"bond {bond.bond_id!r} coupon rate")
        if bond.inflation_indexed:
            _reject_inexact_indexed_period_rate(
                bond.bond_id, rate_ppb, bond.annual_coupon_rate, bond.coupon_period_months
            )
        bonds.append(
            {
                "bond_id": bond.bond_id,
                "agent_id": bond.agent_id,
                "account_id": bond.account_id,
                "issuer_jurisdiction_id": bond.issuer_jurisdiction_id,
                "face_value": int(currency_amount_to_quanta(bond.face_value, quantum=quantum)),
                "purchase_price": int(currency_amount_to_quanta(bond.purchase_price, quantum=quantum)),
                "annual_coupon_rate_ppb": rate_ppb,
                "coupon_period_months": int(bond.coupon_period_months),
                "inflation_indexed": bond.inflation_indexed,
                "purchase_month_index": int(bond.purchase_month_index),
                "maturity_month_index": int(bond.maturity_month_index),
            }
        )
    return bonds


def _target_allocation_policies(scenario: Scenario, *, quantum: Decimal) -> list[dict[str, Any]]:
    return [
        {
            "agent_id": policy.agent_id,
            "account_id": policy.account_id,
            "source_account_ids": list(policy.source_account_ids),
            "sleeves": [
                {
                    "asset_id": _asset_id(sleeve.asset),
                    "weight": int(sleeve.weight),
                    "quantity_scale": quantity_scale_for_asset(sleeve.asset),
                }
                for sleeve in policy.sleeves
            ],
            "cash_floor": _amount(
                policy.cash_floor, quantum=quantum, context=f"target-allocation floor for {policy.agent_id!r}"
            ),
            "cash_ceiling": _amount(
                policy.cash_ceiling, quantum=quantum, context=f"target-allocation ceiling for {policy.agent_id!r}"
            ),
            "cause_id_prefix": policy.cause_id_prefix,
            "purchase_slots_per_sleeve": int(policy.purchase_slots_per_sleeve),
            "rebalance_tolerance_ppb": (
                None if policy.rebalance_tolerance is None else _ppb(policy.rebalance_tolerance)
            ),
        }
        for policy in scenario.target_allocation_policies
    ]


def _property_purchases(scenario: Scenario, *, quantum: Decimal) -> list[dict[str, Any]]:
    return [
        {
            "month": int(purchase.month),
            "cause_id": purchase.cause_id,
            "property_id": purchase.property_id,
            "location_id": purchase.location_id,
            "buyer_agent_id": purchase.buyer_agent_id,
            "buyer_account_id": purchase.buyer_account_id,
            "seller_agent_id": purchase.seller_agent_id,
            "seller_account_id": purchase.seller_account_id,
            "purchase_price": int(currency_amount_to_quanta(purchase.purchase_price, quantum=quantum)),
            "down_payment": int(currency_amount_to_quanta(purchase.down_payment, quantum=quantum)),
            "buyer_closing_cost": int(currency_amount_to_quanta(purchase.buyer_closing_cost, quantum=quantum)),
            "rented_fraction_ppb": _ppb(purchase.rented_fraction),
            "land_value_fraction_ppb": _ppb(purchase.land_value_fraction),
            "mortgage": (
                None
                if purchase.mortgage is None
                else {
                    "liability_id": purchase.mortgage.liability_id,
                    "lender_agent_id": purchase.mortgage.lender_agent_id,
                    "lender_account_id": purchase.mortgage.lender_account_id,
                    "principal": int(currency_amount_to_quanta(purchase.mortgage.principal, quantum=quantum)),
                    "annual_interest_rate_ppb": _exact_ppb(
                        purchase.mortgage.annual_interest_rate,
                        context=f"mortgage {purchase.mortgage.liability_id!r} interest rate",
                    ),
                    "term_months": int(purchase.mortgage.term_months),
                }
            ),
        }
        for purchase in scenario.scheduled_property_purchases
    ]


def _closing_cost_bps(event: PropertySaleEvent) -> int:
    """Seller closing costs, which the fixture spells in basis points and the scenario in percent.

    The engines then reach the retained fraction by different routes — Rust scales proceeds by
    `(10_000 - bps) / 10_000`, JAX by `_round_int64((1 - pct / 100) * PPB) / PPB` — so the
    integer identity between them is checked here rather than assumed.
    """

    exact = Decimal(str(event.closing_cost_pct)) * 100
    if exact != exact.to_integral_value():
        raise UnsupportedScenarioError(
            f"property sale of {event.property_id!r} charges {event.closing_cost_pct}% closing costs, "
            "which is not a whole number of basis points"
        )
    bps = int(exact)
    retained_ppb = MONEY_FACTOR_SCALE - bps * (MONEY_FACTOR_SCALE // _BASIS_POINT_SCALE)
    if _ppb(1.0 - event.closing_cost_pct / 100.0) != retained_ppb:
        raise UnsupportedScenarioError(
            f"property sale of {event.property_id!r} retains a different fraction of its proceeds on each "
            "side of the Python/JAX float64 boundary"
        )
    return bps


def _locations(scenario: Scenario, locations: Mapping[str, Location], *, quantum: Decimal) -> list[dict[str, Any]]:
    """The locations this scenario actually buys a property in.

    The rest of the deployment's catalog is places no property is ever bought, and the fixture's
    location list exists for the property-tax policy to read.
    """

    referenced = sorted({purchase.location_id for purchase in scenario.scheduled_property_purchases})
    return [
        {
            "location_id": location_id,
            "display_name": locations[location_id].display_name,
            "jurisdiction_ids": list(locations[location_id].jurisdiction_ids),
            "annual_property_tax_rate_ppb": _ppb(locations[location_id].annual_property_tax_rate),
            "annual_special_assessment": int(
                currency_amount_to_quanta(locations[location_id].annual_special_assessment, quantum=quantum)
            ),
        }
        for location_id in referenced
    ]


def encode_fixture(
    scenario: Scenario,
    plan: CompiledSimulation,
    *,
    external_series: ExternalSeriesContext,
    jurisdictions: Mapping[str, Jurisdiction],
    locations: Mapping[str, Location],
) -> dict[str, Any]:
    """The strict integer fixture for one compiled simulation.

    `plan` must be `compile_simulation(scenario, ..., external_series, jurisdictions, locations)`:
    the sampled cubes and the compiled tax tables come from it, so the fixture carries the same
    integers the JAX engine would run rather than a second derivation of them. `external_series`
    supplies only the private-equity company-valuation channel, which the compiler drops because
    no engine phase reads it and the Rust validator still requires.
    """

    if plan.horizon_months != int(scenario.horizon_months):
        raise ValueError(f"plan horizon {plan.horizon_months} does not match scenario {scenario.horizon_months}")
    quantum = scenario.currency.quantum
    obligations: tuple[ScheduledObligation | RecurringObligation, ...] = (
        *scenario.scheduled_obligations,
        *scenario.recurring_obligations,
    )
    for obligation in obligations:
        _reject_unsupported_obligation(obligation)
    for sale in scenario.scheduled_asset_sales:
        if sale.price_per_unit is not None:
            raise UnsupportedScenarioError(
                f"scheduled sale {sale.cause_id!r} fixes a price per unit; the Rust engine prices every "
                "sale off the asset's own sampled series"
            )

    lifecycle = scenario.property_lifecycle_events
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "currency_code": scenario.currency.code,
        "currency_quantum": format(quantum, "f"),
        "rollout_count": plan.rollout_count,
        "scenario": {
            "horizon_months": int(scenario.horizon_months),
            "jurisdictions": _jurisdiction_identities(scenario, jurisdictions),
            "locations": _locations(scenario, locations, quantum=quantum),
            "accounts": [
                {
                    "account": _account(balance.agent_id, balance.account_id),
                    "opening_balance": int(currency_amount_to_quanta(balance.balance, quantum=quantum)),
                }
                for balance in scenario.initial_cash
            ],
            "scheduled_transfers": [
                {"month": int(transfer.month)}
                | _flow(transfer, quantum=quantum, context=f"scheduled transfer {transfer.cause_id!r}")
                for transfer in scenario.scheduled_transfers
            ],
            "recurring_transfers": [
                _span(transfer.start_month, transfer.end_month)
                | _flow(transfer, quantum=quantum, context=f"recurring transfer {transfer.cause_id!r}")
                for transfer in scenario.recurring_transfers
            ],
            "scheduled_property_cashflows": [
                {"month": int(cashflow.month), "property_id": cashflow.property_id}
                | _flow(cashflow, quantum=quantum, context=f"scheduled property cashflow {cashflow.cause_id!r}")
                for cashflow in scenario.scheduled_property_cashflows
            ],
            "recurring_property_cashflows": [
                _span(cashflow.start_month, cashflow.end_month)
                | {"property_id": cashflow.property_id}
                | _flow(cashflow, quantum=quantum, context=f"recurring property cashflow {cashflow.cause_id!r}")
                for cashflow in scenario.recurring_property_cashflows
            ],
            "obligations": [
                {"month": int(obligation.month)} | _obligation(obligation, quantum=quantum)
                for obligation in scenario.scheduled_obligations
            ],
            "recurring_obligations": [
                _span(obligation.start_month, obligation.end_month) | _obligation(obligation, quantum=quantum)
                for obligation in scenario.recurring_obligations
            ],
            "initial_lots": _initial_lots(scenario, quantum=quantum),
            "initial_bonds": _initial_bonds(scenario, quantum=quantum),
            "scheduled_sales": [
                {
                    "month": int(sale.month),
                    "cause_id": sale.cause_id,
                    "agent_id": sale.agent_id,
                    "account_id": sale.source_account_id,
                    "asset_id": _asset_id(sale.asset),
                    "units": int(quantity_to_quanta(sale.quantity, scale=quantity_scale_for_asset(sale.asset))),
                    "proceeds_account_id": sale.proceeds_account_id,
                }
                for sale in scenario.scheduled_asset_sales
            ],
            "tax_profiles": _tax_profiles(scenario, plan, jurisdictions),
            "distributions": [
                {
                    "agent_id": distribution.agent_id,
                    "holding_account_id": distribution.holding_account_id,
                    "asset_id": _asset_id(distribution.asset),
                    "to_account_id": distribution.to_account_id,
                    "tax_character": [
                        {
                            "fraction_ppb": _ppb(tax_slice.fraction),
                            "issuer_jurisdiction_id": tax_slice.issuer_jurisdiction_id,
                        }
                        for tax_slice in distribution.tax_character
                    ],
                }
                for distribution in scenario.security_distributions
            ],
            "target_allocation_policies": _target_allocation_policies(scenario, quantum=quantum),
            "private_equity_tender_policies": [
                {
                    "owner_agent_id": policy.owner_agent_id,
                    "proceeds_account_id": policy.proceeds_account_id,
                    "liquid_net_worth_floor": _amount(
                        policy.liquid_net_worth_floor,
                        quantum=quantum,
                        context=f"private-equity floor for {policy.owner_agent_id!r}",
                    ),
                }
                for policy in scenario.private_equity_tender_policies
            ],
            "harvest_policies": [
                {
                    "owner_agent_id": policy.owner_agent_id,
                    "account_id": policy.account_id,
                    "asset_id": _asset_id(policy.asset),
                    "peak_annual_yield_ppb": policy.yield_params.peak_annual_yield_ppb,
                    "floor_annual_yield_ppb": policy.yield_params.floor_annual_yield_ppb,
                    "maturity_decay_exponent_ppb": _exact_ppb(
                        policy.yield_params.maturity_decay_exponent,
                        context=f"harvest policy for {policy.owner_agent_id!r} decay exponent",
                    ),
                    "drawdown_sensitivity_ppb": policy.yield_params.drawdown_sensitivity_ppb,
                    "short_term_fraction_ppb": _ppb(policy.short_term_fraction),
                }
                for policy in scenario.harvest_policies
            ],
            "scheduled_property_purchases": _property_purchases(scenario, quantum=quantum),
            "initial_primary_residences": [
                {"agent_id": assignment.agent_id, "property_id": assignment.property_id}
                for assignment in scenario.initial_primary_residences
            ],
            "primary_residence_events": [
                {"month": int(event.month), "agent_id": event.agent_id, "property_id": event.property_id}
                for event in scenario.primary_residence_events
            ],
            "property_rented_fraction_events": [
                {
                    "month": int(event.month),
                    "property_id": event.property_id,
                    "rented_fraction_ppb": _ppb(event.rented_fraction),
                }
                for event in lifecycle
                if isinstance(event, SetRentedFractionEvent)
            ],
            "capital_improvement_events": [
                {
                    "month": int(event.month),
                    "property_id": event.property_id,
                    "amount": int(currency_amount_to_quanta(event.amount, quantum=quantum)),
                    "description": event.description,
                }
                for event in lifecycle
                if isinstance(event, CapitalImprovementEvent)
            ],
            "property_sales": [
                {
                    "month": int(event.month),
                    "property_id": event.property_id,
                    "closing_cost_bps": _closing_cost_bps(event),
                }
                for event in lifecycle
                if isinstance(event, PropertySaleEvent)
            ],
            "mortgage_interest_deduction_policies": [
                {
                    "liability_id": policy.liability_id,
                    "owner_agent_id": policy.owner_agent_id,
                    "debt_class": policy.debt_class,
                    "per_jurisdiction_principal_cap": {
                        jurisdiction_id: int(currency_amount_to_quanta(cap, quantum=quantum))
                        for jurisdiction_id, cap in policy.per_jurisdiction_principal_cap.items()
                    },
                }
                for policy in scenario.mortgage_interest_deduction_policies
            ],
            "property_tax_policies": [
                {
                    "property_id": policy.property_id,
                    "owner_agent_id": policy.owner_agent_id,
                    "from_account_id": policy.from_account_id,
                    "tax_authority_agent_id": policy.tax_authority_agent_id,
                    "tax_authority_account_id": policy.tax_authority_account_id,
                    "annual_tax_rate_ppb": (None if policy.annual_tax_rate is None else _ppb(policy.annual_tax_rate)),
                    "start_month": int(policy.start_month),
                    "end_month": None if policy.end_month is None else int(policy.end_month),
                }
                for policy in scenario.property_tax_policies
            ],
            "federal_salt_deduction_policies": [
                {
                    "profile_id": policy.profile_id,
                    "federal_jurisdiction_id": policy.federal_jurisdiction_id,
                    "cap_schedule": [
                        {
                            "effective_year_index": int(entry.effective_year_index),
                            "cap": int(currency_amount_to_quanta(entry.cap, quantum=quantum)),
                        }
                        for entry in policy.cap_schedule
                    ],
                }
                for policy in scenario.federal_salt_deduction_policies
            ],
        },
        "series": [*_level_series(plan), *_private_equity_series(plan, external_series.private_equity)],
    }
