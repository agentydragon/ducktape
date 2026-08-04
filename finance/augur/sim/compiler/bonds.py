"""Bond compile output.

Every bond quantity in phase 1 is fixed by the terms: par purchase, held to maturity, no
default and no marking, so nothing a bond does depends on a rollout. The whole schedule is
therefore a **compile-time constant** — `(month, bond)` tables of coupon and redemption
cents, and a `(month, bond)` on-books mask — and bonds need no field in the engine's scan
carry at all. This is why the bond phase is a table lookup rather than per-rollout
arithmetic.

The two cash tables are kept apart because the tax treatment differs and nothing downstream
should have to re-derive which is which: a coupon is `InterestIncome` and accrues to its
issuer's income bucket, while redeeming the face is a return of capital that is not income
at all. At par against a par basis it is not a capital gain either.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.bonds import coupon_amount_cents, coupon_months, is_on_books
from finance.augur.sim.compiler.helpers import NO_CODE, StringTable, slot
from finance.augur.sim.compiler.income_buckets import IncomeBuckets
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.scenario import BondHolding, InterestIncome, Scenario


@dataclass(frozen=True)
class BondCompileOutput:
    """Per-bond identity plus the per-(month, bond) cash and balance-sheet tables."""

    bond_id: NDArray[np.int64]
    agent: NDArray[np.int64]
    to_slot: NDArray[np.int64]
    face: NDArray[np.int64]
    # Income-tensor row this bond's coupon accrues to, or NO_CODE when the holder is untaxed.
    income_row: NDArray[np.int64]
    coupon: NDArray[np.int64]  # (H, bond)
    redemption: NDArray[np.int64]  # (H, bond)
    on_books: NDArray[np.int64]  # (H+1, bond); face is cash by the end of the maturity month


def bond_income_categories(scenario: Scenario) -> set[InterestIncome]:
    """The interest sources bonds contribute to the income-bucket axis.

    Separate from the transfer walk because the axis has to exist before the bond table can
    name a row in it: a Treasury coupon needs a `federal_us` interest bucket whether or not
    any transfer happens to carry one.
    """

    return {InterestIncome(issuer_jurisdiction_id=bond.issuer_jurisdiction_id) for bond in scenario.initial_bonds}


def compile_bonds(
    scenario: Scenario,
    strings: StringTable,
    account_slot_by_key: dict[tuple[str, str], int],
    profile_index_by_agent: dict[str, int],
    buckets: IncomeBuckets,
) -> BondCompileOutput:
    horizon = int(scenario.horizon_months)
    bonds = scenario.initial_bonds
    coupon = np.zeros((horizon, len(bonds)), dtype=np.int64)
    redemption = np.zeros((horizon, len(bonds)), dtype=np.int64)
    on_books = np.zeros((horizon + 1, len(bonds)), dtype=np.int64)

    for index, bond in enumerate(bonds):
        amount = coupon_amount_cents(
            face_value_usd=bond.face_value_usd,
            annual_coupon_rate=bond.annual_coupon_rate,
            coupon_period_months=bond.coupon_period_months,
        )
        for month in coupon_months(
            purchase_month_index=bond.purchase_month_index,
            maturity_month_index=bond.maturity_month_index,
            coupon_period_months=bond.coupon_period_months,
        ):
            # Coupons before month 0 belong to a bond bought before the horizon; they were
            # paid before the simulation starts and are not this scenario's cash.
            if 0 <= month < horizon:
                coupon[month, index] = amount
        if 0 <= bond.maturity_month_index < horizon:
            redemption[bond.maturity_month_index, index] = usd_to_cents(bond.face_value_usd)
        for month in range(horizon + 1):
            on_books[month, index] = is_on_books(
                month_index=month,
                purchase_month_index=bond.purchase_month_index,
                maturity_month_index=bond.maturity_month_index,
            )

    return BondCompileOutput(
        bond_id=np.asarray([strings.require(bond.bond_id) for bond in bonds], dtype=np.int64),
        agent=np.asarray([strings.require(bond.agent_id) for bond in bonds], dtype=np.int64),
        to_slot=np.asarray(
            [slot(account_slot_by_key, bond.agent_id, bond.account_id) for bond in bonds], dtype=np.int64
        ),
        face=np.asarray([usd_to_cents(bond.face_value_usd) for bond in bonds], dtype=np.int64),
        income_row=np.asarray([_income_row(bond, profile_index_by_agent, buckets) for bond in bonds], dtype=np.int64),
        coupon=coupon,
        redemption=redemption,
        on_books=on_books,
    )


def _income_row(bond: BondHolding, profile_index_by_agent: dict[str, int], buckets: IncomeBuckets) -> int:
    return buckets.bucket(
        profile_index_by_agent.get(bond.agent_id, NO_CODE),
        InterestIncome(issuer_jurisdiction_id=bond.issuer_jurisdiction_id),
    )
