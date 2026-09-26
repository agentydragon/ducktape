"""Integer discriminators for scenario income categories."""

from __future__ import annotations

from enum import IntEnum


class IncomeCategory(IntEnum):
    """What KIND of income a dollar is. One category per distinct tax behavior.

    Qualified dividends are singled out because they take the long-term capital-gain rates
    wherever a jurisdiction has them, and ordinary rates elsewhere.

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
    QUALIFIED_DIVIDEND = 2
