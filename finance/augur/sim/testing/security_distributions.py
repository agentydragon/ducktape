"""Shared authored fund-payout cases for compiler and action-session controls."""

from __future__ import annotations

from decimal import Decimal

from finance.augur.model.series import SecurityDistributionKey, SecurityKey, SecuritySymbol
from finance.augur.sim.scenario import DistributionTaxSlice, InitialLot, SecurityDistribution
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import checking, taxed

HORIZON = 13
SYMBOL = SecuritySymbol("bnd")
FUND = SecurityKey(symbol=SYMBOL)
UNITS = 10_000.0
PRICE = Decimal(73)
# A round monthly payout per unit, so `units x per unit` is exact at every split.
PER_UNIT = Decimal("0.20")
# Below half a cent a unit: what a bond fund at a low unit price pays at a low yield, and what
# rounding the RATE to the currency quantum turned into a literal zero (#5832).
SUB_QUANTUM_PER_UNIT = Decimal("0.0004")
# Between one and two cents a unit, where quantizing the rate to whole cents does not zero the
# payout but still loses a fifth of it.
LOSSY_PER_UNIT = Decimal("0.0123")


def payout_quanta(per_unit: Decimal) -> int:
    return int(Decimal(str(UNITS)) * per_unit * 100)


MONTHLY_PAYOUT_QUANTA = payout_quanta(PER_UNIT)

TREASURY = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="federal_us"),)
CALIFORNIA_MUNI = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="california"),)
CORPORATE = (DistributionTaxSlice(fraction=1.0),)
# An aggregate fund: part Treasury, part corporate. The case a single tag cannot express.
AGGREGATE = (
    DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),
    DistributionTaxSlice(fraction=0.6),
)
TREASURY_SHARE, CORPORATE_SHARE = Decimal("0.4"), Decimal("0.6")
# Snapshot `m` opens month `m`, so month 11 has months 0..10 behind it: eleven payouts.
YEAR_END, PAYOUTS_BY_YEAR_END = 11, 11


def distribution_case(
    *,
    tax_character: tuple[DistributionTaxSlice, ...] = TREASURY,
    is_taxed: bool = True,
    distributes: bool = True,
    holding_account_id: str = "brokerage",
    pays_a_series: bool = True,
    per_unit: Decimal = PER_UNIT,
) -> Case:
    """Alice holds one fund in a brokerage account and its payout lands in checking.

    `is_taxed=False` leaves the payout standing alone in the cash channel, which the
    cashflow cases want: with a tax profile the year-end settlement lands in the same months.
    """

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(50_000)), ("irs", Decimal(0))),
            initial_lots=[
                InitialLot(
                    lot_id="bnd-lot",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=FUND,
                    purchase_month_index=-24,
                    quantity=UNITS,
                    cost_basis=Decimal(str(UNITS)) * PRICE,
                )
            ],
            security_distributions=[
                SecurityDistribution(
                    asset=FUND,
                    agent_id="alice",
                    holding_account_id=holding_account_id,
                    to_account_id="checking",
                    tax_character=tax_character,
                )
            ]
            if distributes
            else [],
            tax_profiles=[taxed("alice", "federal_us", "california")] if is_taxed else [],
            horizon_months=HORIZON,
        ),
        rollout_count=1,
        series={
            FUND: flat(PRICE, rollout_count=1, horizon_months=HORIZON),
            # The two series have the same shape and units, which is the point of the payout
            # being a primitive rather than a rate.
            **(
                {SecurityDistributionKey(symbol=SYMBOL): flat(per_unit, rollout_count=1, horizon_months=HORIZON)}
                if pays_a_series
                else {}
            ),
        },
    )
