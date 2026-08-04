"""Property + Liability (mortgage) compile outputs. Pairs with `codec/properties.py`
and `codec/liabilities.py`.

Property purchases and their optional mortgages are compiled in lockstep — the
mortgage rows in `LiabilityCompileOutput` cross-reference the property row in
`PropertyCompileOutput` via `property_slot`, and `PropertyCompileOutput.mortgage_slot`
points back. They live in one module because the producer (`compile_properties_and_liabilities`)
emits both in a single pass."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.compiler.helpers import NO_CODE, AccountSlots, StringTable
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.locations import Location
from finance.augur.sim.runtime import mortgage_monthly_payment_usd
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class PropertyCompileOutput:
    """Per-(month, slot) property-purchase plumbing produced alongside liabilities.
    `cause` is the (month × slot) event log; everything else is per-slot scalar shape.
    `mortgage_slot[idx]` is NO_CODE for cash purchases; otherwise the index into the
    parallel `LiabilityCompileOutput` arrays."""

    cause: NDArray[np.int64]
    id: NDArray[np.int64]
    location_id: NDArray[np.int64]
    location_tax_rate: NDArray[np.float64]
    special_assessment_annual_usd: NDArray[np.int64]
    initial_assessed_value: NDArray[np.int64]
    month: NDArray[np.int64]
    buyer_agent: NDArray[np.int64]
    buyer_account: NDArray[np.int64]
    buyer_slot: NDArray[np.int64]
    seller_agent: NDArray[np.int64]
    seller_account: NDArray[np.int64]
    seller_slot: NDArray[np.int64]
    purchase_price: NDArray[np.int64]
    closing_cost: NDArray[np.int64]
    down_payment: NDArray[np.int64]
    adjusted_basis: NDArray[np.int64]
    stake_contribution: NDArray[np.int64]
    equity_ledger: NDArray[np.int64]
    mortgage_slot: NDArray[np.int64]


@dataclass(frozen=True)
class LiabilityCompileOutput:
    """Per-liability arrays (one row per scheduled-purchase mortgage). `property_slot[i]`
    points back into `PropertyCompileOutput` so the engine can look up the underlying
    property when settling a mortgage payment or computing MID."""

    codes: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    agent: NDArray[np.int64]
    payment_account: NDArray[np.int64]
    payment_slot: NDArray[np.int64]
    counterparty_agent: NDArray[np.int64]
    counterparty_account: NDArray[np.int64]
    counterparty_slot: NDArray[np.int64]
    principal: NDArray[np.int64]
    annual_rate: NDArray[np.float64]
    term_months: NDArray[np.int64]
    monthly_payment: NDArray[np.int64]


def compile_properties_and_liabilities(
    scenario: Scenario, strings: StringTable, account_slot_by_key: AccountSlots, locations: dict[str, Location]
) -> tuple[PropertyCompileOutput, LiabilityCompileOutput]:
    prop_count = len(scenario.scheduled_property_purchases)
    cause = np.full((int(scenario.horizon_months), max(1, prop_count)), NO_CODE, dtype=np.int64)
    prop_id = np.zeros(max(1, prop_count), dtype=np.int64)
    location_id = np.zeros(max(1, prop_count), dtype=np.int64)
    location_tax_rate = np.zeros(max(1, prop_count), dtype=np.float64)
    special_assessment_annual_usd = np.zeros(max(1, prop_count), dtype=np.int64)
    initial_assessed_value = np.zeros(max(1, prop_count), dtype=np.int64)
    month_array = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    buyer_agent = np.zeros(max(1, prop_count), dtype=np.int64)
    buyer_account = np.zeros(max(1, prop_count), dtype=np.int64)
    buyer_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    seller_agent = np.zeros(max(1, prop_count), dtype=np.int64)
    seller_account = np.zeros(max(1, prop_count), dtype=np.int64)
    seller_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)
    purchase_price = np.zeros(max(1, prop_count), dtype=np.int64)
    closing_cost = np.zeros(max(1, prop_count), dtype=np.int64)
    down_payment = np.zeros(max(1, prop_count), dtype=np.int64)
    adjusted_basis = np.zeros(max(1, prop_count), dtype=np.int64)
    stake_contribution = np.zeros(max(1, prop_count), dtype=np.int64)
    equity_ledger = np.zeros(max(1, prop_count), dtype=np.int64)
    mortgage_slot = np.full(max(1, prop_count), NO_CODE, dtype=np.int64)

    liability_codes: list[int] = []
    liability_property_slot: list[int] = []
    liability_agent: list[int] = []
    liability_payment_account: list[int] = []
    liability_payment_slot: list[int] = []
    liability_counterparty_agent: list[int] = []
    liability_counterparty_account: list[int] = []
    liability_counterparty_slot: list[int] = []
    liability_principal: list[np.int64] = []
    liability_rate: list[float] = []
    liability_term: list[int] = []
    liability_payment: list[np.int64] = []

    for idx, purchase in enumerate(scenario.scheduled_property_purchases):
        cause[purchase.month, idx] = strings.require(purchase.cause_id)
        prop_id[idx] = strings.require(purchase.property_id)
        location_id[idx] = strings.require(purchase.location_id)
        location = locations.get(purchase.location_id)
        if location is None:
            known_location_ids = ", ".join(repr(location_id) for location_id in sorted(locations)) or "<none>"
            raise ValueError(
                f"scheduled property purchase {purchase.cause_id!r} references unknown location_id "
                f"{purchase.location_id!r}; known location ids: {known_location_ids}"
            )
        location_tax_rate[idx] = float(location.annual_property_tax_rate)
        special_assessment_annual_usd[idx] = usd_to_cents(location.annual_special_assessment_usd)
        initial_assessed_value[idx] = usd_to_cents(purchase.purchase_price_usd)
        month_array[idx] = int(purchase.month)
        buyer_agent[idx] = strings.require(purchase.buyer_agent_id)
        buyer_account[idx] = strings.require(purchase.buyer_account_id)
        buyer_slot[idx] = account_slot_by_key.resolve(purchase.buyer_agent_id, purchase.buyer_account_id)
        seller_agent[idx] = strings.require(purchase.seller_agent_id)
        seller_account[idx] = strings.require(purchase.seller_account_id)
        seller_slot[idx] = account_slot_by_key.resolve(purchase.seller_agent_id, purchase.seller_account_id)
        mortgage_principal = purchase.mortgage.principal_usd if purchase.mortgage is not None else 0.0
        purchase_price[idx] = usd_to_cents(purchase.purchase_price_usd)
        closing_cost[idx] = usd_to_cents(purchase.buyer_closing_cost_usd)
        down_payment[idx] = usd_to_cents(purchase.down_payment_usd)
        adjusted_basis[idx] = usd_to_cents(purchase.purchase_price_usd + purchase.buyer_closing_cost_usd)
        stake_contribution[idx] = usd_to_cents(purchase.down_payment_usd + purchase.buyer_closing_cost_usd)
        equity_ledger[idx] = usd_to_cents(purchase.purchase_price_usd - mortgage_principal)
        if purchase.mortgage is not None:
            mortgage_slot[idx] = len(liability_codes)
            mortgage = purchase.mortgage
            liability_codes.append(strings.require(mortgage.liability_id))
            liability_property_slot.append(idx)
            liability_agent.append(strings.require(purchase.buyer_agent_id))
            liability_payment_account.append(strings.require(purchase.buyer_account_id))
            liability_payment_slot.append(
                account_slot_by_key.resolve(purchase.buyer_agent_id, purchase.buyer_account_id)
            )
            liability_counterparty_agent.append(strings.require(mortgage.lender_agent_id))
            liability_counterparty_account.append(strings.require(mortgage.lender_account_id))
            liability_counterparty_slot.append(
                account_slot_by_key.resolve(mortgage.lender_agent_id, mortgage.lender_account_id)
            )
            liability_principal.append(usd_to_cents(mortgage.principal_usd))
            liability_rate.append(float(mortgage.annual_interest_rate))
            liability_term.append(int(mortgage.term_months))
            liability_payment.append(
                usd_to_cents(
                    mortgage_monthly_payment_usd(
                        mortgage.principal_usd, mortgage.annual_interest_rate, int(mortgage.term_months)
                    )
                )
            )

    return (
        PropertyCompileOutput(
            cause=cause,
            id=prop_id,
            location_id=location_id,
            location_tax_rate=location_tax_rate,
            special_assessment_annual_usd=special_assessment_annual_usd,
            initial_assessed_value=initial_assessed_value,
            month=month_array,
            buyer_agent=buyer_agent,
            buyer_account=buyer_account,
            buyer_slot=buyer_slot,
            seller_agent=seller_agent,
            seller_account=seller_account,
            seller_slot=seller_slot,
            purchase_price=purchase_price,
            closing_cost=closing_cost,
            down_payment=down_payment,
            adjusted_basis=adjusted_basis,
            stake_contribution=stake_contribution,
            equity_ledger=equity_ledger,
            mortgage_slot=mortgage_slot,
        ),
        LiabilityCompileOutput(
            codes=np.asarray(liability_codes, dtype=np.int64),
            property_slot=np.asarray(liability_property_slot, dtype=np.int64),
            agent=np.asarray(liability_agent, dtype=np.int64),
            payment_account=np.asarray(liability_payment_account, dtype=np.int64),
            payment_slot=np.asarray(liability_payment_slot, dtype=np.int64),
            counterparty_agent=np.asarray(liability_counterparty_agent, dtype=np.int64),
            counterparty_account=np.asarray(liability_counterparty_account, dtype=np.int64),
            counterparty_slot=np.asarray(liability_counterparty_slot, dtype=np.int64),
            principal=np.asarray(liability_principal, dtype=np.int64),
            annual_rate=np.asarray(liability_rate, dtype=np.float64),
            term_months=np.asarray(liability_term, dtype=np.int64),
            monthly_payment=np.asarray(liability_payment, dtype=np.int64),
        ),
    )
