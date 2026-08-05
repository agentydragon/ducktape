"""Compile-side plan: SlotPlan, CompiledSimulation, compile_simulation. Pairs with
`codec/plan.py` (SimulationRun) at the engine boundary.

`compile_simulation` is the orchestrator that interns strings, builds the shared
index maps, calls every per-domain `compile_*` helper, and assembles the
`CompiledSimulation` plan the engine consumes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId
from finance.augur.product.asset_key import AssetKey, asset_price_key_or_none
from finance.augur.sim.compiler.assets import PurchaseCompileOutput, SaleCompileOutput, compile_purchases, compile_sales
from finance.augur.sim.compiler.bonds import BondCompileOutput, compile_bonds
from finance.augur.sim.compiler.deductions import (
    MIDCompileOutput,
    SaltCompileOutput,
    compile_federal_salt_deductions,
    compile_mortgage_interest_deductions,
)
from finance.augur.sim.compiler.helpers import (
    EXTERNAL_ACCOUNT_ID,
    EXTERNAL_AGENT_ID,
    NO_CODE,
    AccountSlots,
    AssetTable,
    StringTable,
)
from finance.augur.sim.compiler.lifecycle import LifecycleEventCompileOutput, compile_lifecycle_events
from finance.augur.sim.compiler.liquidity import LiquidityPolicyCompileOutput, compile_liquidity_policies
from finance.augur.sim.compiler.obligations import ObligationCompileOutput, compile_obligation_slots
from finance.augur.sim.compiler.primary_residence import PrimaryResidenceEventCompileOutput, compile_primary_residences
from finance.augur.sim.compiler.private_equity import (
    PEChannels,
    PEIssuerCompileOutput,
    PEPolicyCompileOutput,
    compile_pe_channels,
    compile_private_equity_tenders,
)
from finance.augur.sim.compiler.properties import (
    LiabilityCompileOutput,
    PropertyCompileOutput,
    compile_properties_and_liabilities,
)
from finance.augur.sim.compiler.property_cashflows import PropertyCashflowCompileOutput, compile_property_cashflows
from finance.augur.sim.compiler.series import collect_level_series_keys, external_values_cube
from finance.augur.sim.compiler.target_allocation import (
    TargetAllocationCompileOutput,
    compile_target_allocation_policies,
)
from finance.augur.sim.compiler.tax import (
    TaxCompileOutput,
    TaxLiabilityCompileOutput,
    compile_capital_gain_agents,
    compile_tax,
    compile_tax_liability_slots,
)
from finance.augur.sim.compiler.tlh_harvest import HarvestPolicyCompileOutput, compile_harvest_policies
from finance.augur.sim.compiler.transfers import TransferCompileOutput, compile_transfer_slots
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, usd_to_cents
from finance.augur.sim.jurisdictions import Jurisdiction
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import PropertySaleEvent, Scenario


@dataclass(frozen=True)
class SlotPlan:
    """Dense shape contract for one compiled simulation.

    Dimensions use the notation from `augur/plans/dense_shape_discipline.md`.
    Counts that can be absent but are still iterated by engine phases use their
    allocated sentinel axis size, usually `max(1, actual_count)`.
    """

    event_months: int
    snapshot_months: int
    rollout_count: int
    agent_count: int
    cash_count: int
    lot_count: int
    bond_count: int
    tax_profile_count: int
    # Rows of the YTD income tensor: one per (profile, income source), so a jurisdiction can
    # include a wage dollar and exclude a muni coupon for the same agent.
    income_bucket_count: int
    capital_gain_agent_count: int
    tax_link_count: int
    tax_liability_count: int
    property_count: int
    liability_count: int
    max_transfer_slots: int
    max_property_cashflow_slots: int
    max_obligation_slots: int
    scheduled_sale_count: int
    liquidity_policy_count: int
    max_liquidity_policy_assets: int
    pe_issuer_count: int
    # Count of reduced-form TLH harvest policies (`max(1, len(scenario.harvest_policies))`); the
    # sentinel row when there are none carries an empty lot mask the engine skips.
    harvest_policy_count: int
    max_tax_settlement_slots: int


@dataclass(frozen=True)
class CompiledSimulation:
    horizon_months: int
    rollout_count: int
    slot_plan: SlotPlan
    strings: tuple[str, ...]
    # Typed asset identity for each lot/sale/chain asset code (`lot_asset_codes`,
    # `sales.asset`, `liquidity_policies.assets`). Decode lifts those codes back to `AssetKey`.
    assets: tuple[AssetKey, ...]
    # Typed level-series identity for each row of `external_values` (the dense price cube);
    # the row index is `series_index_by_id[key]`. PE marks live in `pe_channels`, not here.
    series_keys: tuple[LevelSeriesKey, ...]
    external_values: NDArray[np.float64]
    agent_codes: NDArray[np.int64]
    cash_agent_codes: NDArray[np.int64]
    cash_account_codes: NDArray[np.int64]
    cash_initial_balance: NDArray[np.int64]
    lot_id_codes: NDArray[np.int64]
    lot_agent_codes: NDArray[np.int64]
    lot_account_codes: NDArray[np.int64]
    lot_asset_codes: NDArray[np.int64]
    # Per-lot index into `external_values` for the lot's pricing series. NO_CODE for lots
    # whose asset_id has no registered sampled level (defensive: shouldn't normally happen
    # for holdings, but the sentinel keeps lookups safe).
    lot_asset_series_index: NDArray[np.int64]
    lot_purchase_month: NDArray[np.int64]
    lot_cost_basis_per_unit: NDArray[np.int64]
    lot_initial_quantity: NDArray[np.int64]
    lot_quantity_scale: NDArray[np.int64]
    tax: TaxCompileOutput
    capital_gain_agent_codes: NDArray[np.int64]
    tax_profile_capital_gain_index: NDArray[np.int64]
    mid: MIDCompileOutput
    salt: SaltCompileOutput
    tax_liabilities: TaxLiabilityCompileOutput
    # Row of the cash array the rest of the world settles on. It is the LAST row, so slicing
    # `[:external_cash_slot]` gives exactly the agents' own accounts.
    external_cash_slot: int
    transfers: TransferCompileOutput
    property_cashflows: PropertyCashflowCompileOutput
    bonds: BondCompileOutput
    properties: PropertyCompileOutput
    # Per-property rented_fraction (0..1). Primary-residence use is tracked separately per agent.
    # Drives MID/SALT/Schedule E splits + monthly depreciation accrual.
    property_rented_fraction: NDArray[np.float64]
    # Per-property depreciable building basis = purchase_price × (1 - land_value_fraction) +
    # buyer_closing_cost. Land is non-depreciable; the 27.5-year SL clock applies only to the
    # building portion. Capitalized closing costs add to the depreciable basis.
    property_building_basis: NDArray[np.int64]
    # Profile index of each property's owner (buyer_agent_id → tax profile). NO_CODE if the
    # owner has no tax profile. Used to route Schedule E depreciation deductions.
    property_owner_profile_index: NDArray[np.int64]
    # Agent slot of each property's owner/buyer. Used to resolve the agent's current
    # primary-residence assignment for Section 121 qualifying-use accrual.
    property_owner_agent_index: NDArray[np.int64]
    # Series index of each property's home_value series, used at sale time to compute market
    # value. NO_CODE only for properties without sale events whose series was not supplied.
    property_home_value_series_index: NDArray[np.int64]
    initial_primary_residence_property_index: NDArray[np.int64]
    primary_residence_events: PrimaryResidenceEventCompileOutput
    lifecycle_events: LifecycleEventCompileOutput
    liabilities: LiabilityCompileOutput
    # Profile index of each liability's owner. NO_CODE if the owner has no tax profile.
    liability_owner_profile_index: NDArray[np.int64]
    sales: SaleCompileOutput
    purchases: PurchaseCompileOutput
    obligations: ObligationCompileOutput
    # Per-PE-issuer arrays. Issuers are the distinct `private_equity:<issuer>` asset_ids
    # appearing in `initial_lots`. For each issuer:
    #   - the event-series index identifying its tender-opportunity stream (NO_CODE if no
    #     event series is registered for it — issuer never tenders within the sim horizon)
    #   - the level-series index for its sampled mark (used both for portfolio valuation and
    #     for sale-proceeds = units * mark at tender)
    #   - the policy index (into the per-policy arrays below) whose LNW-floor governs sales
    #     on tenders for this issuer (NO_CODE if no PrivateEquityTenderPolicy applies)
    pe_issuers: PEIssuerCompileOutput
    pe_policies: PEPolicyCompileOutput
    pe_channels: PEChannels
    # Reduced-form TLH harvest policies (Piece 2b). Per-policy yield curve + lot mask + the
    # owner's capital-gain agent index + the index series index driving the period return.
    # Consumed by the engine's `_apply_tlh_harvest` phase and the sale-time basis give-back.
    harvest_policies: HarvestPolicyCompileOutput
    liquidity_policies: LiquidityPolicyCompileOutput
    target_allocation_policies: TargetAllocationCompileOutput


def lot_order_for_pool(
    *,
    lot_agent_codes: NDArray[np.int64],
    lot_account_codes: NDArray[np.int64],
    lot_asset_codes: NDArray[np.int64],
    lot_purchase_month: NDArray[np.int64],
    lot_id_codes: NDArray[np.int64],
    agent_code: int,
    account_code: int,
    asset_code: int,
) -> NDArray[np.int64]:
    """Return FIFO-ordered lot indices for one `(agent, account, asset)` pool.

    Lot identity and purchase month are plan columns, so the order is static: the engine
    resolves it once host-side and the traced step gathers by the resulting indices. Lots in
    different accounts are not fungible, which is why the account code is part of the key."""

    eligible = np.flatnonzero(
        (lot_agent_codes == agent_code) & (lot_account_codes == account_code) & (lot_asset_codes == asset_code)
    )
    order = np.lexsort((lot_id_codes[eligible], lot_purchase_month[eligible]))
    return eligible[order]


def compile_simulation(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    jurisdictions: dict[str, Jurisdiction],
    locations: dict[str, Location],
) -> CompiledSimulation:
    strings = StringTable()
    assets = AssetTable()
    horizon = int(scenario.horizon_months)

    account_slot_by_key: dict[tuple[str, str], int] = {}
    cash_agent_codes: list[int] = []
    cash_account_codes: list[int] = []
    cash_initial_balance: list[np.int64] = []
    for entry in scenario.initial_cash:
        key = (entry.agent_id, entry.account_id)
        if key in account_slot_by_key:
            raise ValueError(f"duplicate initial cash account: {entry.agent_id}/{entry.account_id}")
        account_slot_by_key[key] = len(cash_initial_balance)
        cash_agent_codes.append(strings.require(entry.agent_id))
        cash_account_codes.append(strings.require(entry.account_id))
        cash_initial_balance.append(usd_to_cents(entry.balance_usd))

    # One more cash row than the scenario declares: the rest of the world. Every counterparty
    # the scenario does not model settles here, so no flow is discarded and total cash across
    # all rows is conserved. It opens at zero and goes steeply negative, which is what a contra
    # account funding every paycheck is supposed to do.
    external_slot = len(cash_initial_balance)
    cash_agent_codes.append(strings.require(EXTERNAL_AGENT_ID))
    cash_account_codes.append(strings.require(EXTERNAL_ACCOUNT_ID))
    cash_initial_balance.append(np.int64(0))
    account_slots = AccountSlots(by_key=account_slot_by_key, external=external_slot)

    agent_slot_by_id: dict[str, int] = {}
    agent_codes: list[int] = []
    for agent in scenario.agents:
        agent_slot_by_id[agent.agent_id] = len(agent_codes)
        agent_codes.append(strings.require(agent.agent_id))

    series_keys = collect_level_series_keys(scenario, external_series)
    series_index_by_id = {key: idx for idx, key in enumerate(series_keys)}
    _reject_missing_property_sale_home_values(scenario, external_series)
    external_values = external_values_cube(
        external_series, series_index_by_id=series_index_by_id, rollout_count=rollout_count, horizon_months=horizon
    )

    profile_index_by_agent = {profile.agent_id: idx for idx, profile in enumerate(scenario.tax_profiles)}
    tax = compile_tax(scenario, strings, account_slots, jurisdictions)
    (capital_gain_agent_codes, tax_profile_capital_gain_index) = compile_capital_gain_agents(scenario, strings)

    tax_liabilities = compile_tax_liability_slots(horizon, tax)

    properties, liabilities = compile_properties_and_liabilities(scenario, strings, account_slots, locations)

    # Per-liability rented_fraction: each liability is tied to one property via
    # liabilities.property_slot; the property's rented_fraction (0..1) drives both the MID
    # scale-down (MID applies only to owner-use share = 1 - rented_fraction) and the
    # Schedule E rental-interest deduction (= rented_fraction × interest_ytd).
    property_count = len(scenario.scheduled_property_purchases)
    property_slot_by_id: dict[str, int] = {
        p.property_id: i for i, p in enumerate(scenario.scheduled_property_purchases)
    }
    transfers = compile_transfer_slots(
        scenario, strings, account_slots, profile_index_by_agent, series_index_by_id, tax.buckets
    )
    property_cashflows = compile_property_cashflows(
        scenario, strings, account_slots, profile_index_by_agent, series_index_by_id, property_slot_by_id, tax.buckets
    )
    bonds = compile_bonds(scenario, strings, account_slots, profile_index_by_agent, tax.buckets, series_index_by_id)
    property_rented_fraction = np.array(
        [float(p.rented_fraction) for p in scenario.scheduled_property_purchases], dtype=np.float64
    )
    # Building basis = (purchase price × (1 - land_fraction)) + capitalized closing costs.
    property_building_basis = np.array(
        [
            usd_to_cents(
                float(p.purchase_price_usd) * (1.0 - float(p.land_value_fraction)) + float(p.buyer_closing_cost_usd)
            )
            for p in scenario.scheduled_property_purchases
        ],
        dtype=np.int64,
    )
    property_owner_profile_index = np.array(
        [profile_index_by_agent.get(p.buyer_agent_id, NO_CODE) for p in scenario.scheduled_property_purchases],
        dtype=np.int64,
    )
    property_owner_agent_index = np.array(
        [agent_slot_by_id.get(p.buyer_agent_id, NO_CODE) for p in scenario.scheduled_property_purchases], dtype=np.int64
    )
    property_home_value_series_index = np.array(
        [
            series_index_by_id.get(HomeValueKey(location_id=LocationId(p.location_id)), NO_CODE)
            for p in scenario.scheduled_property_purchases
        ],
        dtype=np.int64,
    )
    initial_primary_residence_property_index, primary_residence_events = compile_primary_residences(
        scenario, agent_slot_by_id=agent_slot_by_id, property_slot_by_id=property_slot_by_id
    )
    # Allocate min-shape arrays for the no-property scenario so downstream callers can index
    # `property_building_basis[max(1, property_count)]` without special-casing.
    if property_count == 0:
        property_rented_fraction = np.zeros(1, dtype=np.float64)
        property_building_basis = np.zeros(1, dtype=np.int64)
        property_owner_profile_index = np.full(1, NO_CODE, dtype=np.int64)
        property_owner_agent_index = np.full(1, NO_CODE, dtype=np.int64)
        property_home_value_series_index = np.full(1, NO_CODE, dtype=np.int64)

    lifecycle_events = compile_lifecycle_events(scenario, property_slot_by_id)

    liability_owner_profile_index = np.array(
        [
            profile_index_by_agent.get(strings.values[int(liabilities.agent[lia])], NO_CODE)
            for lia in range(liabilities.codes.shape[0])
        ],
        dtype=np.int64,
    )

    mid = compile_mortgage_interest_deductions(scenario, strings, tax=tax, liabilities=liabilities)
    salt = compile_federal_salt_deductions(scenario, strings, tax=tax)

    sales = compile_sales(scenario, strings, assets, account_slots, series_index_by_id)

    obligations = compile_obligation_slots(
        scenario, strings, account_slots, series_index_by_id, properties, property_slot_by_id, liabilities, tax
    )

    liquidity_policies = compile_liquidity_policies(scenario, strings, assets, account_slots, series_index_by_id)
    target_allocation_policies = compile_target_allocation_policies(
        scenario, strings, assets, account_slots, series_index_by_id
    )
    # CLEANUP(added 2026-08-05): Delete this gate when the engine grows the target-allocation
    #   phase that reads `plan.target_allocation_policies` (D10 step 4c in
    #   <finance/augur/plans/actor_actions.md>). Until then a configured policy would compile
    #   and then do nothing at all — a knob that silently ignores you is worse than a missing
    #   feature, so it fails loudly instead.
    if scenario.target_allocation_policies:
        raise NotImplementedError(
            "target-allocation policies compile but no engine phase executes them yet, so this "
            "scenario would run as if the policy were absent — every obligation the account "
            "cannot already cover would fail while sellable assets sat untouched. Use "
            "`liquidity_policies` until the target-allocation phase lands."
        )

    lot_id_codes: list[int] = []
    lot_agent_codes: list[int] = []
    lot_account_codes: list[int] = []
    lot_asset_codes: list[int] = []
    lot_purchase_month: list[int] = []
    lot_cost_basis_per_unit: list[np.int64] = []
    lot_initial_quantity: list[np.int64] = []
    lot_quantity_scale: list[int] = []
    for lot in scenario.initial_lots:
        scale = quantity_scale_for_asset(lot.asset)
        lot_id_codes.append(strings.require(lot.lot_id))
        lot_agent_codes.append(strings.require(lot.agent_id))
        lot_account_codes.append(strings.require(lot.account_id))
        lot_asset_codes.append(assets.require(lot.asset))
        lot_purchase_month.append(int(lot.purchase_month_index))
        lot_cost_basis_per_unit.append(usd_to_cents(lot.cost_basis_per_unit_usd))
        lot_initial_quantity.append(quantity_to_quanta(lot.quantity, scale=scale))
        lot_quantity_scale.append(scale)

    # One empty lot slot per scheduled purchase, appended after the initial lots. The slot
    # carries its real purchase month, so the compile-time FIFO order is already right: the
    # slot holds zero units until that month, and a zero-quantity lot contributes nothing to
    # a FIFO walk that reaches it early.
    purchases = compile_purchases(
        scenario, strings, assets, account_slots, series_index_by_id, first_lot_slot=len(scenario.initial_lots)
    )
    for purchase in scenario.scheduled_asset_purchases:
        lot_id_codes.append(strings.require(purchase.lot_id))
        lot_agent_codes.append(strings.require(purchase.agent_id))
        lot_account_codes.append(strings.require(purchase.to_account_id))
        lot_asset_codes.append(assets.require(purchase.asset))
        lot_purchase_month.append(int(purchase.month))
        # Basis is per-rollout from here on: the engine promotes this column to `(lot, rollout)`
        # carry state and the purchase writes the realized price into its slot.
        lot_cost_basis_per_unit.append(np.int64(0))
        lot_initial_quantity.append(np.int64(0))
        lot_quantity_scale.append(quantity_scale_for_asset(purchase.asset))

    lot_agent_codes_arr = np.asarray(lot_agent_codes, dtype=np.int64)
    lot_asset_codes_arr = np.asarray(lot_asset_codes, dtype=np.int64)
    # PE-guard: PE lots are priced by `pe_channels` marks, not the price cube, so they have no
    # asset-price series (`asset_price_key_or_none` → None → NO_CODE).
    lot_asset_series_index = np.asarray(
        [
            NO_CODE
            if (price_key := asset_price_key_or_none(asset)) is None
            else series_index_by_id.get(price_key, NO_CODE)
            for asset in (
                *(lot.asset for lot in scenario.initial_lots),
                *(purchase.asset for purchase in scenario.scheduled_asset_purchases),
            )
        ],
        dtype=np.int64,
    )
    # Built from the slot list, not from `scenario.initial_cash`: the external account is a
    # real row with no scenario entry, and rebuilding from the config would drop it and leave
    # this array one shorter than the cash tensor.
    cash_agent_codes_arr = np.asarray(cash_agent_codes, dtype=np.int64)
    pe_issuers, pe_policies = compile_private_equity_tenders(
        scenario,
        strings,
        asset_table=assets,
        series_index_by_id=series_index_by_id,
        lot_agent_codes=lot_agent_codes_arr,
        lot_asset_codes=lot_asset_codes_arr,
        cash_agent_codes=cash_agent_codes_arr,
    )
    pe_channels = compile_pe_channels(
        pe_issuers, private_equity=external_series.private_equity, rollout_count=rollout_count, horizon_months=horizon
    )

    # Reduced-form TLH harvest policies. Pass the intern lookups (`strings.require` / `assets.require`)
    # so the policy's (owner, account, asset) lot mask is matched against the exact codes the lot
    # table was built from above.
    harvest_policies = compile_harvest_policies(
        scenario,
        series_index_by_id=series_index_by_id,
        lot_agent_codes=lot_agent_codes_arr,
        lot_account_codes=np.asarray(lot_account_codes, dtype=np.int64),
        lot_asset_codes=lot_asset_codes_arr,
        capital_gain_agent_codes=capital_gain_agent_codes,
        string_code_of=strings.require,
        asset_code_of=assets.require,
    )

    slot_plan = SlotPlan(
        event_months=horizon,
        snapshot_months=horizon + 1,
        rollout_count=rollout_count,
        agent_count=len(agent_codes),
        cash_count=len(cash_initial_balance),
        lot_count=len(lot_id_codes),
        bond_count=len(scenario.initial_bonds),
        tax_profile_count=tax.profile_agent.shape[0],
        income_bucket_count=tax.buckets.row_count,
        capital_gain_agent_count=capital_gain_agent_codes.shape[0],
        tax_link_count=max(1, tax.link_profile.shape[0]),
        tax_liability_count=tax_liabilities.profile_index.shape[0],
        property_count=properties.month.shape[0],
        liability_count=liabilities.codes.shape[0],
        max_transfer_slots=transfers.cause.shape[1],
        max_property_cashflow_slots=property_cashflows.cause.shape[1],
        max_obligation_slots=obligations.cause.shape[1],
        scheduled_sale_count=sales.month.shape[0],
        liquidity_policy_count=liquidity_policies.assets.shape[0],
        max_liquidity_policy_assets=liquidity_policies.assets.shape[1],
        pe_issuer_count=pe_issuers.codes.shape[0],
        harvest_policy_count=harvest_policies.gain_profile_index.shape[0],
        max_tax_settlement_slots=max(1, len(scenario.tax_profiles)),
    )

    return CompiledSimulation(
        horizon_months=horizon,
        rollout_count=rollout_count,
        slot_plan=slot_plan,
        strings=tuple(strings.values),
        assets=tuple(assets.values),
        series_keys=series_keys,
        external_values=external_values,
        agent_codes=np.asarray(agent_codes, dtype=np.int64),
        cash_agent_codes=np.asarray(cash_agent_codes, dtype=np.int64),
        cash_account_codes=np.asarray(cash_account_codes, dtype=np.int64),
        cash_initial_balance=np.asarray(cash_initial_balance, dtype=np.int64),
        lot_id_codes=np.asarray(lot_id_codes, dtype=np.int64),
        lot_agent_codes=np.asarray(lot_agent_codes, dtype=np.int64),
        lot_account_codes=np.asarray(lot_account_codes, dtype=np.int64),
        lot_asset_codes=np.asarray(lot_asset_codes, dtype=np.int64),
        lot_asset_series_index=lot_asset_series_index,
        lot_purchase_month=np.asarray(lot_purchase_month, dtype=np.int64),
        lot_cost_basis_per_unit=np.asarray(lot_cost_basis_per_unit, dtype=np.int64),
        lot_initial_quantity=np.asarray(lot_initial_quantity, dtype=np.int64),
        lot_quantity_scale=np.asarray(lot_quantity_scale, dtype=np.int64),
        tax=tax,
        capital_gain_agent_codes=capital_gain_agent_codes,
        tax_profile_capital_gain_index=tax_profile_capital_gain_index,
        mid=mid,
        salt=salt,
        tax_liabilities=tax_liabilities,
        external_cash_slot=external_slot,
        transfers=transfers,
        property_cashflows=property_cashflows,
        bonds=bonds,
        properties=properties,
        liabilities=liabilities,
        # Ordinary-bucket ROWS, not profile indices: these scatter deductions into the YTD
        # income tensor, whose rows are (profile, source) pairs.
        liability_owner_profile_index=tax.buckets.ordinary_rows(liability_owner_profile_index),
        property_rented_fraction=property_rented_fraction,
        property_building_basis=property_building_basis,
        property_owner_profile_index=tax.buckets.ordinary_rows(property_owner_profile_index),
        property_owner_agent_index=property_owner_agent_index,
        property_home_value_series_index=property_home_value_series_index,
        initial_primary_residence_property_index=initial_primary_residence_property_index,
        primary_residence_events=primary_residence_events,
        lifecycle_events=lifecycle_events,
        sales=sales,
        purchases=purchases,
        obligations=obligations,
        pe_issuers=pe_issuers,
        pe_policies=pe_policies,
        pe_channels=pe_channels,
        harvest_policies=harvest_policies,
        liquidity_policies=liquidity_policies,
        target_allocation_policies=target_allocation_policies,
    )


def _reject_missing_property_sale_home_values(scenario: Scenario, external_series: ExternalSeriesContext) -> None:
    """Property sales need an explicit external home-value path for their location."""

    if not scenario.property_lifecycle_events:
        return
    property_by_id = {property_.property_id: property_ for property_ in scenario.scheduled_property_purchases}
    available = external_series.levels.series_keys()
    for event in scenario.property_lifecycle_events:
        if not isinstance(event, PropertySaleEvent):
            continue
        property_ = property_by_id[event.property_id]
        required = HomeValueKey(location_id=LocationId(property_.location_id))
        if required in available:
            continue
        msg = (
            f"property sale for property_id {event.property_id!r} at month {event.month} requires external "
            f"home-value series {required.wire_id!r}"
        )
        raise KeyError(msg)
