"""Shared fund-payout terms for the compiler and action-session controls."""

from __future__ import annotations

from decimal import Decimal

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.scenario import DistributionTaxSlice

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

TREASURY = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id=JurisdictionId("federal_us")),)
CALIFORNIA_MUNI = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id=JurisdictionId("california")),)
CORPORATE = (DistributionTaxSlice(fraction=1.0),)
# An aggregate fund: part Treasury, part corporate. The case a single tag cannot express.
AGGREGATE = (
    DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id=JurisdictionId("federal_us")),
    DistributionTaxSlice(fraction=0.6),
)
TREASURY_SHARE, CORPORATE_SHARE = Decimal("0.4"), Decimal("0.6")
# Snapshot `m` opens month `m`, so month 11 has months 0..10 behind it: eleven payouts.
YEAR_END, PAYOUTS_BY_YEAR_END = 11, 11
