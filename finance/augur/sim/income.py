"""Typed income and deduction categories: what kind of taxable dollar a transfer carries."""

from enum import StrEnum
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


class InterestCharacterKind(StrEnum):
    TREASURY = "treasury"
    MUNICIPAL = "municipal"
    TAXABLE = "taxable"


class Treasury(BaseModel):
    """US Treasury bills, notes, bonds and TIPS."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[InterestCharacterKind.TREASURY] = InterestCharacterKind.TREASURY


class Municipal(BaseModel):
    """A tax-exempt obligation of a state or its political subdivisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[InterestCharacterKind.MUNICIPAL] = InterestCharacterKind.MUNICIPAL
    state: JurisdictionId = Field(
        description=(
            "The state whose obligation this is, directly or through a political subdivision. Only "
            "the taxing jurisdictions' own rules read it; it need not name a declared jurisdiction."
        )
    )


class Taxable(BaseModel):
    """Interest no jurisdiction exempts: corporate bonds, bank deposits, ..."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[InterestCharacterKind.TAXABLE] = InterestCharacterKind.TAXABLE


type InterestCharacter = Annotated[Treasury | Municipal | Taxable, Field(discriminator="kind")]


class InterestIncome(BaseModel):
    """Interest, tagged with the legal regime of the obligation that pays it.

    Whether a jurisdiction taxes this dollar is that jurisdiction's rule for the character
    (`sim.tax.taxes_interest_from`), so the same California muni coupon is exempt for a
    Californian and taxable for a New Yorker without the instrument changing.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal[IncomeCategory.INTEREST] = IncomeCategory.INTEREST
    character: InterestCharacter


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


def _interest_label(character: InterestCharacter) -> str:
    if isinstance(character, Municipal):
        return f"{character.kind}:{character.state}"
    return character.kind


def income_source_sort_key(category: TransferIncomeCategory) -> tuple[int, str]:
    """Ordinary first, then qualified dividends, then interest by character, taxable last."""

    if isinstance(category, InterestIncome):
        character = category.character
        return (3, "") if isinstance(character, Taxable) else (2, _interest_label(character))
    if isinstance(category, QualifiedDividendIncome):
        return (1, "")
    return (0, "")


def income_source_wire_id(category: TransferIncomeCategory) -> str:
    """The financial income ledger's label for one prepared category."""

    if isinstance(category, InterestIncome):
        return f"interest:{_interest_label(category.character)}"
    if isinstance(category, QualifiedDividendIncome):
        return "qualified_dividend"
    return "ordinary"
