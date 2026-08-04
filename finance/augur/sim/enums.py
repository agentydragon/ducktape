"""Simulation-wide IntEnum types for array-indexed discriminators.

These are IntEnum (not StrEnum) because the values index into numpy arrays
and must remain plain integers for arithmetic and indexing."""

from __future__ import annotations

from enum import IntEnum


class ObligationSource(IntEnum):
    CONFIGURED_OBLIGATION = 0
    MORTGAGE_PAYMENT = 1
    PROPERTY_TAX = 2
    ESTIMATED_TAX = 3
    ESTIMATED_TAX_Q4 = 4
    TAX_TRUE_UP = 5


class IncomeCategory(IntEnum):
    """What KIND of ordinary income a dollar is. Only two, because only two BEHAVE differently.

    Interest is singled out because jurisdictions disagree about it: a Treasury coupon is
    federal-taxable but state-exempt, a California muni coupon is exempt in California and
    federally, a New York muni coupon is federally exempt but California-TAXABLE. Wages are
    taxed by everyone.

    Note what is NOT here: `in_state` / `out_of_state` variants. "In-state" is not a property
    of a bond — a California muni is in-state for a Californian and out-of-state for a New
    Yorker. Interest therefore carries its ISSUING jurisdiction, and each jurisdiction's rules
    decide (see `Jurisdiction.taxes_interest_from`). "In-state" is the derived relation
    `issuer == me`, never a stored label.
    """

    ORDINARY = 0
    INTEREST = 1


class CapitalGainClassification(IntEnum):
    LONG_TERM = 0
    SHORT_TERM = 1


class LifecycleKind(IntEnum):
    FRACTION = 0
    CAPITAL_IMPROVEMENT = 1
    SALE = 2


class PrivateEquityDispositionKind(IntEnum):
    TENDER = 0
    PUBLIC_MARKET = 1
    FORCED_SALE = 2
    FORCED_RECOVERY = 3


class PrivateEquityOpportunityOutcome(IntEnum):
    SOLD = 0
    FLOOR_SATISFIED = 1
    CAPACITY_ZERO = 2
    LIQUIDITY_BLOCKED = 3
    NO_POLICY = 4
    NO_UNITS = 5
    NONPOSITIVE_MARK = 6
