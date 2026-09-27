"""Typed income and deduction categories: what kind of taxable dollar a transfer carries."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance.augur.sim.enums import IncomeCategory
from finance.augur.sim.ids import JurisdictionId


class OrdinaryIncome(BaseModel):
    """Wages, rent, and everything else every jurisdiction taxes.

    Frozen because the tag is a value, not a record: the compiler puts these in a set to
    derive the income-bucket axis, so two `OrdinaryIncome()` must be one key.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.ORDINARY] = IncomeCategory.ORDINARY


class InterestIncome(BaseModel):
    """Interest, tagged with WHO ISSUED the debt — never with whether it is "in-state".

    Whether a jurisdiction taxes this dollar is a relation between the issuer and that
    jurisdiction (`Jurisdiction.taxes_interest_from`), so the same California muni coupon is
    exempt for a Californian and taxable for a New Yorker without the instrument changing.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.INTEREST] = IncomeCategory.INTEREST
    issuer_jurisdiction_id: JurisdictionId | None = Field(
        default=None,
        description=(
            "The taxing authority that issued the debt — `federal_us` for a Treasury, "
            "`california` for a CA muni. `None` means a non-governmental issuer (a corporate "
            "bond), which no jurisdiction exempts."
        ),
    )


# TODO: apply the holding-period test (IRC 1(h)(11)(B)(iii)) to the holder's lots rather than
# trusting the declaration.
class QualifiedDividendIncome(BaseModel):
    """Dividends taxed at the long-term capital-gain rates where a jurisdiction has them, else as ordinary.

    Declared simplification: the declaration says "qualified" and the engine trusts it; the
    holding-period test (IRC 1(h)(11)(B)(iii)) is not checked.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.QUALIFIED_DIVIDEND] = IncomeCategory.QUALIFIED_DIVIDEND


type TransferIncomeCategory = Annotated[
    OrdinaryIncome | InterestIncome | QualifiedDividendIncome, Field(discriminator="category")
]
ORDINARY_INCOME = OrdinaryIncome()

type TransferDeductionCategory = Literal["ordinary"]


def income_source_sort_key(category: TransferIncomeCategory) -> tuple[int, str]:
    """Ordinary first, then qualified dividends, then interest by issuer, corporate last."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return (2, issuer) if issuer is not None else (3, "")
    if isinstance(category, QualifiedDividendIncome):
        return (1, "")
    return (0, "")


def income_source_wire_id(category: TransferIncomeCategory) -> str:
    """The financial income ledger's label for one prepared category."""

    if isinstance(category, InterestIncome):
        issuer = category.issuer_jurisdiction_id
        return f"interest:{issuer if issuer is not None else 'corporate'}"
    if isinstance(category, QualifiedDividendIncome):
        return "qualified_dividend"
    return "ordinary"
