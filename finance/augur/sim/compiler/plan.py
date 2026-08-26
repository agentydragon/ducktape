"""Compile-side plan: SlotPlan, CompiledSimulation, compile_simulation. Pairs with
`codec/plan.py` (SimulationRun) at the engine boundary.

`compile_simulation` is the orchestrator that interns strings, builds the shared
index maps, calls every per-domain `compile_*` helper, and assembles the
`CompiledSimulation` plan the engine consumes."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

import numpy as np
from jaxtyping import Float64, Int64

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId
from finance.augur.product.asset_key import AssetKey, asset_price_key_or_none
from finance.augur.sim.compiler.assets import SaleCompileOutput, compile_sales
from finance.augur.sim.compiler.bonds import BondCompileOutput, compile_bonds
from finance.augur.sim.compiler.cashflows import CashflowCompileOutput, compile_cashflows
from finance.augur.sim.compiler.deductions import (
    MIDCompileOutput,
    SaltCompileOutput,
    compile_federal_salt_deductions,
    compile_mortgage_interest_deductions,
)
from finance.augur.sim.compiler.distributions import DistributionCompileOutput, compile_distributions
from finance.augur.sim.compiler.helpers import (
    EXTERNAL_ACCOUNT_ID,
    EXTERNAL_AGENT_ID,
    NO_CODE,
    AccountSlots,
    AssetTable,
    StringTable,
)
from finance.augur.sim.compiler.lifecycle import LifecycleEventCompileOutput, compile_lifecycle_events
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
from finance.augur.sim.compiler.series import (
    collect_level_series_keys,
    external_series_cubes,
    materialize_level_rows,
    validate_series_indexed_amounts,
)
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
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
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
    cash_count: int
    lot_count: int
    tax_profile_count: int
    # Rows of the YTD income tensor: one per (profile, income source), so a jurisdiction can
    # include a wage dollar and exclude a muni coupon for the same agent.
    income_bucket_count: int
    capital_gain_agent_count: int
    tax_link_count: int
    tax_liability_count: int
    property_count: int
    liability_count: int
    max_cashflow_slots: int
    max_obligation_slots: int
    scheduled_sale_count: int
    target_allocation_policy_count: int
    max_target_allocation_sleeves: int
    pe_issuer_count: int
    # Count of reduced-form TLH harvest policies (`max(1, len(scenario.harvest_policies))`); the
    # sentinel row when there are none carries an empty lot mask the engine skips.
    harvest_policy_count: int
    max_tax_settlement_slots: int


@dataclass(frozen=True)
class CompiledSimulation:
    horizon_months: int
    rollout_count: int
    currency_code: str
    currency_quantum: Decimal
    slot_plan: SlotPlan
    strings: tuple[str, ...]
    # Typed asset identity for each lot/sale/chain asset code (`lot_asset_codes`,
    # `sales.asset`, `target_allocation_policies.sleeve_assets`). Decode lifts those codes back
    # to `AssetKey`.
    assets: tuple[AssetKey, ...]
    # Typed level-series identity for each row of the external cubes; the row index is
    # `series_index_by_id[key]`. PE marks live in `pe_channels`, not here.
    series_keys: tuple[LevelSeriesKey, ...]
    # Heterogeneous model levels (CPI and other non-money ratios remain float-valued).
    external_values: Float64[np.ndarray, " series rollout snapshot"]
    # Price-like model levels quantized to integer scenario-currency quanta before sim.
    external_money_values: Int64[np.ndarray, " series rollout snapshot"]
    agent_codes: Int64[np.ndarray, " agent"]
    cash_agent_codes: Int64[np.ndarray, " cash"]
    cash_account_codes: Int64[np.ndarray, " cash"]
    cash_initial_balance: Int64[np.ndarray, " cash"]
    lot_id_codes: Int64[np.ndarray, " lot"]
    lot_agent_codes: Int64[np.ndarray, " lot"]
    lot_account_codes: Int64[np.ndarray, " lot"]
    lot_asset_codes: Int64[np.ndarray, " lot"]
    # Per-lot index into `external_values` for the lot's pricing series. NO_CODE for lots
    # whose asset_id has no registered sampled level (defensive: shouldn't normally happen
    # for holdings, but the sentinel keeps lookups safe).
    lot_asset_series_index: Int64[np.ndarray, " lot"]
    # Month-0 value of the engine's per-rollout purchase month. Static only until a lot is
    # bought: a policy-chosen purchase writes the month its rollout actually paid.
    lot_purchase_month: Int64[np.ndarray, " lot"]
    # What FIFO sorts by. Separate from the month because a slot a policy will fill has no
    # compile-time month, yet its position in the sale order is fixed: slots fill
    # monotonically, so the rank is known even when the month is not.
    lot_fifo_rank: Int64[np.ndarray, " lot"]
    # `(policy, sleeve, slot)` lot indices a target-allocation policy may buy into, NO_CODE
    # where a policy has fewer sleeves or a smaller budget than the dense shape.
    target_allocation_purchase_slots: Int64[np.ndarray, " policy sleeve purchase_slot"]
    lot_cost_basis_per_unit: Int64[np.ndarray, " lot"]
    lot_initial_quantity: Int64[np.ndarray, " lot"]
    lot_quantity_scale: Int64[np.ndarray, " lot"]
    tax: TaxCompileOutput
    capital_gain_agent_codes: Int64[np.ndarray, " capital_gain_profile"]
    tax_profile_capital_gain_index: Int64[np.ndarray, " tax_profile"]
    mid: MIDCompileOutput
    salt: SaltCompileOutput
    tax_liabilities: TaxLiabilityCompileOutput
    # Row of the cash array the rest of the world settles on. It is the LAST row, so slicing
    # `[:external_cash_slot]` gives exactly the agents' own accounts.
    external_cash_slot: int
    cashflows: CashflowCompileOutput
    bonds: BondCompileOutput
    distributions: DistributionCompileOutput
    properties: PropertyCompileOutput
    # Per-property rented_fraction (0..1). Primary-residence use is tracked separately per agent.
    # Drives MID/SALT/Schedule E splits + monthly depreciation accrual.
    property_rented_fraction: Float64[np.ndarray, " property"]
    # Per-property depreciable building basis = purchase_price × (1 - land_value_fraction) +
    # buyer_closing_cost. Land is non-depreciable; the 27.5-year SL clock applies only to the
    # building portion. Capitalized closing costs add to the depreciable basis.
    property_building_basis: Int64[np.ndarray, " property"]
    # Profile index of each property's owner (buyer_agent_id → tax profile). NO_CODE if the
    # owner has no tax profile. Used by lifecycle tax-policy lookups.
    property_owner_profile_index: Int64[np.ndarray, " property"]
    # Ordinary-income bucket row for each property's owner. This is deliberately distinct
    # from the profile index: one profile can own multiple jurisdiction/source rows, and the
    # engine scatters Schedule E depreciation into the ordinary-income bucket axis.
    property_owner_ordinary_row: Int64[np.ndarray, " property"]
    # Agent slot of each property's owner/buyer. Used to resolve the agent's current
    # primary-residence assignment for Section 121 qualifying-use accrual.
    property_owner_agent_index: Int64[np.ndarray, " property"]
    # Series index of each property's home_value series, used at sale time to compute market
    # value. NO_CODE only for properties without sale events whose series was not supplied.
    property_home_value_series_index: Int64[np.ndarray, " property"]
    initial_primary_residence_property_index: Int64[np.ndarray, " agent"]
    primary_residence_events: PrimaryResidenceEventCompileOutput
    lifecycle_events: LifecycleEventCompileOutput
    liabilities: LiabilityCompileOutput
    # Profile index of each liability's owner. NO_CODE if the owner has no tax profile.
    liability_owner_profile_index: Int64[np.ndarray, " liability"]
    sales: SaleCompileOutput
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
    target_allocation_policies: TargetAllocationCompileOutput


class _LotRow(NamedTuple):
    """One complete host-side lot row before dense column materialization."""

    lot_id_code: int
    agent_code: int
    account_code: int
    asset_code: int
    asset: AssetKey
    purchase_month: int
    fifo_rank: int
    cost_basis_per_unit: int
    initial_quantity: int
    quantity_scale: int


class _LotTable(NamedTuple):
    lot_id_codes: Int64[np.ndarray, " lot"]
    agent_codes: Int64[np.ndarray, " lot"]
    account_codes: Int64[np.ndarray, " lot"]
    asset_codes: Int64[np.ndarray, " lot"]
    assets: tuple[AssetKey, ...]
    purchase_month: Int64[np.ndarray, " lot"]
    fifo_rank: Int64[np.ndarray, " lot"]
    cost_basis_per_unit: Int64[np.ndarray, " lot"]
    initial_quantity: Int64[np.ndarray, " lot"]
    quantity_scale: Int64[np.ndarray, " lot"]


def _materialize_lots(rows: list[_LotRow]) -> _LotTable:
    """Materialize complete lot rows as aligned int64 columns once."""
    numeric = np.asarray(
        [
            (
                row.lot_id_code,
                row.agent_code,
                row.account_code,
                row.asset_code,
                row.purchase_month,
                row.fifo_rank,
                row.cost_basis_per_unit,
                row.initial_quantity,
                row.quantity_scale,
            )
            for row in rows
        ],
        dtype=np.int64,
    ).reshape(len(rows), 9)
    return _LotTable(
        lot_id_codes=numeric[:, 0],
        agent_codes=numeric[:, 1],
        account_codes=numeric[:, 2],
        asset_codes=numeric[:, 3],
        assets=tuple(row.asset for row in rows),
        purchase_month=numeric[:, 4],
        fifo_rank=numeric[:, 5],
        cost_basis_per_unit=numeric[:, 6],
        initial_quantity=numeric[:, 7],
        quantity_scale=numeric[:, 8],
    )


def lot_order_for_pool(
    *,
    lot_agent_codes: Int64[np.ndarray, " lot"],
    lot_account_codes: Int64[np.ndarray, " lot"],
    lot_asset_codes: Int64[np.ndarray, " lot"],
    lot_fifo_rank: Int64[np.ndarray, " lot"],
    lot_id_codes: Int64[np.ndarray, " lot"],
    agent_code: int,
    account_code: int,
    asset_code: int,
) -> Int64[np.ndarray, " lot"]:
    """Return FIFO-ordered lot indices for one `(agent, account, asset)` pool.

    Lot identity and FIFO rank are plan columns, so the order is static: the engine resolves
    it once host-side and the traced step gathers by the resulting indices. Lots in different
    accounts are not fungible, which is why the account code is part of the key.

    Rank rather than purchase month, because the two stopped being the same thing once a
    purchase month became per-rollout. A slot a policy will fill has no compile-time month,
    but it does have a fixed place in the order — slots fill monotonically, so a later slot
    is always a later purchase, in every rollout."""

    eligible = np.flatnonzero(
        (lot_agent_codes == agent_code) & (lot_account_codes == account_code) & (lot_asset_codes == asset_code)
    )
    order = np.lexsort((lot_id_codes[eligible], lot_fifo_rank[eligible]))
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

    def currency_amount(value: object) -> np.int64:
        return currency_amount_to_quanta(value, quantum=scenario.currency.quantum)

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
        cash_initial_balance.append(currency_amount(entry.balance))

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

    level_rows = materialize_level_rows(
        tuple(external_series.levels.value_rows()), rollout_count=rollout_count, horizon_months=horizon
    )
    series_keys = collect_level_series_keys(scenario, level_rows)
    series_index_by_id = {key: idx for idx, key in enumerate(series_keys)}
    external_values, external_money_values = external_series_cubes(
        level_rows,
        series_index_by_id=series_index_by_id,
        rollout_count=rollout_count,
        horizon_months=horizon,
        currency_quantum=scenario.currency.quantum,
    )
    validate_series_indexed_amounts(
        scenario, rollout_count=rollout_count, rows_by_key={rows.key: rows for rows in level_rows}
    )
    _reject_missing_property_sale_home_values(scenario, frozenset(rows.key for rows in level_rows))

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
    cashflows = compile_cashflows(
        scenario, strings, account_slots, profile_index_by_agent, series_index_by_id, property_slot_by_id, tax.buckets
    )
    bonds = compile_bonds(scenario, strings, account_slots, profile_index_by_agent, tax.buckets, series_index_by_id)
    property_rented_fraction = np.array(
        [float(p.rented_fraction) for p in scenario.scheduled_property_purchases], dtype=np.float64
    )
    # Building basis = (purchase price × (1 - land_fraction)) + capitalized closing costs.
    property_building_basis = np.array(
        [
            currency_amount(
                p.purchase_price * (Decimal(1) - Decimal(str(p.land_value_fraction))) + p.buyer_closing_cost
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
        scenario,
        strings,
        account_slots,
        series_index_by_id,
        properties,
        property_slot_by_id,
        liabilities,
        tax,
        tax_liabilities,
    )

    target_allocation_policies = compile_target_allocation_policies(
        scenario, strings, assets, account_slots, series_index_by_id
    )

    lot_rows: list[_LotRow] = []
    for lot in scenario.initial_lots:
        scale = quantity_scale_for_asset(lot.asset)
        lot_rows.append(
            _LotRow(
                lot_id_code=strings.require(lot.lot_id),
                agent_code=strings.require(lot.agent_id),
                account_code=strings.require(lot.account_id),
                asset_code=assets.require(lot.asset),
                asset=lot.asset,
                purchase_month=int(lot.purchase_month_index),
                fifo_rank=int(lot.purchase_month_index),
                cost_basis_per_unit=int(currency_amount(lot.cost_basis_per_unit)),
                initial_quantity=int(quantity_to_quanta(lot.quantity, scale=scale)),
                quantity_scale=scale,
            )
        )

    # Purchase slots for the target-allocation policies: `purchase_slots_per_sleeve` empty lots
    # per sleeve, so a policy that buys has somewhere to put what it bought. Each purchase needs
    # its OWN lot — a lot bought in a different month has a different holding period and a
    # different basis — and lots are a dense axis, so the count is configured rather than grown.
    #
    # The rank is above every real month and increases with the slot, which is what lets the
    # sale order stay compile-time derivable: a sleeve's slots fill in cursor order, so a later
    # slot is a later purchase in every rollout even though its month is not known here.
    max_slots = max((p.purchase_slots_per_sleeve for p in scenario.target_allocation_policies), default=0)
    ta_purchase_slots = np.full(
        (
            max(1, len(scenario.target_allocation_policies)),
            target_allocation_policies.sleeve_assets.shape[1],
            max(1, max_slots),
        ),
        NO_CODE,
        dtype=np.int64,
    )
    for policy_idx, policy in enumerate(scenario.target_allocation_policies):
        # Bought lots join the pool the sleeve already sells from, so a purchase and a later
        # sale of the same sleeve meet in one FIFO walk rather than two disjoint pools.
        account_id = policy.source_account_ids[0] if policy.source_account_ids else policy.account_id
        for sleeve_idx, sleeve in enumerate(policy.sleeves):
            for k in range(policy.purchase_slots_per_sleeve):
                slot = len(lot_rows)
                ta_purchase_slots[policy_idx, sleeve_idx, k] = slot
                lot_rows.append(
                    _LotRow(
                        lot_id_code=strings.require(f"{policy.cause_id_prefix}_buy_p{policy_idx}_s{sleeve_idx}_{k}"),
                        agent_code=strings.require(policy.agent_id),
                        account_code=strings.require(account_id),
                        asset_code=assets.require(sleeve.asset),
                        asset=sleeve.asset,
                        # Month 0 until the purchase writes the month its rollout actually paid.
                        # An unfilled slot holds zero units, so nothing ever reads it.
                        purchase_month=0,
                        fifo_rank=scenario.horizon_months + slot,
                        cost_basis_per_unit=0,
                        initial_quantity=0,
                        quantity_scale=quantity_scale_for_asset(sleeve.asset),
                    )
                )

    lot_table = _materialize_lots(lot_rows)
    # After every lot list is complete, deliberately: a distribution pays on the pool's units
    # including the purchase slots a policy has not filled yet, so its mask has to see them.
    distributions = compile_distributions(
        scenario,
        strings,
        assets,
        account_slots,
        profile_index_by_agent,
        tax.buckets,
        series_index_by_id,
        lot_agent_codes=lot_table.agent_codes,
        lot_account_codes=lot_table.account_codes,
        lot_asset_codes=lot_table.asset_codes,
        lot_quantity_scale=lot_table.quantity_scale,
    )
    # PE-guard: PE lots are priced by `pe_channels` marks, not the price cube, so they have no
    # asset-price series (`asset_price_key_or_none` → None → NO_CODE).
    lot_asset_series_index = np.asarray(
        [
            NO_CODE
            if (price_key := asset_price_key_or_none(asset)) is None
            else series_index_by_id.get(price_key, NO_CODE)
            for asset in lot_table.assets
        ],
        dtype=np.int64,
    )
    # Built from the slot list, not from `scenario.initial_cash`: the external account is a
    # real row with no scenario entry, and rebuilding from the config would drop it and leave
    # this array one shorter than the cash tensor.
    cash_agent_codes_arr = np.asarray(cash_agent_codes, dtype=np.int64)
    cash_account_codes_arr = np.asarray(cash_account_codes, dtype=np.int64)
    pe_issuers, pe_policies = compile_private_equity_tenders(
        scenario,
        strings,
        asset_table=assets,
        series_index_by_id=series_index_by_id,
        lot_agent_codes=lot_table.agent_codes,
        lot_asset_codes=lot_table.asset_codes,
        cash_agent_codes=cash_agent_codes_arr,
        cash_account_codes=cash_account_codes_arr,
    )
    pe_channels = compile_pe_channels(
        pe_issuers,
        private_equity=external_series.private_equity,
        rollout_count=rollout_count,
        horizon_months=horizon,
        currency_quantum=scenario.currency.quantum,
    )

    # Reduced-form TLH harvest policies. Pass the intern lookups (`strings.require` / `assets.require`)
    # so the policy's (owner, account, asset) lot mask is matched against the exact codes the lot
    # table was built from above.
    harvest_policies = compile_harvest_policies(
        scenario,
        series_index_by_id=series_index_by_id,
        lot_agent_codes=lot_table.agent_codes,
        lot_account_codes=lot_table.account_codes,
        lot_asset_codes=lot_table.asset_codes,
        capital_gain_agent_codes=capital_gain_agent_codes,
        string_code_of=strings.require,
        asset_code_of=assets.require,
    )

    slot_plan = SlotPlan(
        event_months=horizon,
        snapshot_months=horizon + 1,
        rollout_count=rollout_count,
        cash_count=len(cash_initial_balance),
        lot_count=lot_table.lot_id_codes.shape[0],
        tax_profile_count=tax.profile_agent.shape[0],
        income_bucket_count=tax.buckets.row_count,
        capital_gain_agent_count=capital_gain_agent_codes.shape[0],
        tax_link_count=max(1, tax.link_profile.shape[0]),
        tax_liability_count=tax_liabilities.profile_index.shape[0],
        property_count=properties.month.shape[0],
        liability_count=liabilities.codes.shape[0],
        max_cashflow_slots=cashflows.cause.shape[1],
        max_obligation_slots=obligations.metadata.cause.shape[1],
        scheduled_sale_count=sales.month.shape[0],
        target_allocation_policy_count=target_allocation_policies.sleeve_assets.shape[0],
        max_target_allocation_sleeves=target_allocation_policies.sleeve_assets.shape[1],
        pe_issuer_count=pe_issuers.codes.shape[0],
        harvest_policy_count=harvest_policies.gain_profile_index.shape[0],
        max_tax_settlement_slots=max(1, len(scenario.tax_profiles)),
    )

    return CompiledSimulation(
        horizon_months=horizon,
        rollout_count=rollout_count,
        currency_code=scenario.currency.code,
        currency_quantum=scenario.currency.quantum,
        slot_plan=slot_plan,
        strings=tuple(strings.values),
        assets=tuple(assets.values),
        series_keys=series_keys,
        external_values=external_values,
        external_money_values=external_money_values,
        agent_codes=np.asarray(agent_codes, dtype=np.int64),
        cash_agent_codes=np.asarray(cash_agent_codes, dtype=np.int64),
        cash_account_codes=cash_account_codes_arr,
        cash_initial_balance=np.asarray(cash_initial_balance, dtype=np.int64),
        lot_id_codes=lot_table.lot_id_codes,
        lot_agent_codes=lot_table.agent_codes,
        lot_account_codes=lot_table.account_codes,
        lot_asset_codes=lot_table.asset_codes,
        lot_asset_series_index=lot_asset_series_index,
        lot_purchase_month=lot_table.purchase_month,
        lot_fifo_rank=lot_table.fifo_rank,
        target_allocation_purchase_slots=ta_purchase_slots,
        lot_cost_basis_per_unit=lot_table.cost_basis_per_unit,
        lot_initial_quantity=lot_table.initial_quantity,
        lot_quantity_scale=lot_table.quantity_scale,
        tax=tax,
        capital_gain_agent_codes=capital_gain_agent_codes,
        tax_profile_capital_gain_index=tax_profile_capital_gain_index,
        mid=mid,
        salt=salt,
        tax_liabilities=tax_liabilities,
        external_cash_slot=external_slot,
        cashflows=cashflows,
        bonds=bonds,
        distributions=distributions,
        properties=properties,
        liabilities=liabilities,
        # Ordinary-bucket ROWS, not profile indices: these scatter deductions into the YTD
        # income tensor, whose rows are (profile, source) pairs.
        liability_owner_profile_index=tax.buckets.ordinary_rows(liability_owner_profile_index),
        property_rented_fraction=property_rented_fraction,
        property_building_basis=property_building_basis,
        property_owner_profile_index=property_owner_profile_index,
        property_owner_ordinary_row=tax.buckets.ordinary_rows(property_owner_profile_index),
        property_owner_agent_index=property_owner_agent_index,
        property_home_value_series_index=property_home_value_series_index,
        initial_primary_residence_property_index=initial_primary_residence_property_index,
        primary_residence_events=primary_residence_events,
        lifecycle_events=lifecycle_events,
        sales=sales,
        obligations=obligations,
        pe_issuers=pe_issuers,
        pe_policies=pe_policies,
        pe_channels=pe_channels,
        harvest_policies=harvest_policies,
        target_allocation_policies=target_allocation_policies,
    )


def _reject_missing_property_sale_home_values(scenario: Scenario, available: frozenset[LevelSeriesKey]) -> None:
    """Property sales need an explicit external home-value path for their location."""

    if not scenario.property_lifecycle_events:
        return
    property_by_id = {property_.property_id: property_ for property_ in scenario.scheduled_property_purchases}
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
