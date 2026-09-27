"""Shared fund-payout terms for the distribution tests."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.income import InterestIncome, QualifiedDividendIncome, TransferIncomeCategory

HORIZON = 13
SYMBOL = SecuritySymbol("bnd")
FUND = SecurityKey(symbol=SYMBOL)
UNITS = 10_000
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
    return int(UNITS * per_unit * 100)


MONTHLY_PAYOUT_QUANTA = payout_quanta(PER_UNIT)

# Each fund's split of a payout by income category, in parts per billion.
TREASURY: Mapping[TransferIncomeCategory, int] = {
    InterestIncome(issuer_jurisdiction_id=JurisdictionId("federal_us")): MONEY_FACTOR_SCALE
}
CALIFORNIA_MUNI: Mapping[TransferIncomeCategory, int] = {
    InterestIncome(issuer_jurisdiction_id=JurisdictionId("california")): MONEY_FACTOR_SCALE
}
CORPORATE: Mapping[TransferIncomeCategory, int] = {InterestIncome(): MONEY_FACTOR_SCALE}
# An aggregate fund: part Treasury, part corporate. The case a single tag cannot express.
AGGREGATE: Mapping[TransferIncomeCategory, int] = {
    InterestIncome(issuer_jurisdiction_id=JurisdictionId("federal_us")): 400_000_000,
    InterestIncome(): 600_000_000,
}
TREASURY_SHARE, CORPORATE_SHARE = Decimal("0.4"), Decimal("0.6")
QUALIFIED_DIVIDENDS: Mapping[TransferIncomeCategory, int] = {QualifiedDividendIncome(): MONEY_FACTOR_SCALE}
# Snapshot `m` opens month `m`, so month 11 has months 0..10 behind it: eleven payouts.
YEAR_END, PAYOUTS_BY_YEAR_END = 11, 11
