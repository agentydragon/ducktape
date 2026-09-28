"""Tax law as one tree of jurisdictions loaded from YAML.

Each jurisdiction names its parent (`federal_us` → `california` → a city or a county tax rate
area) and holds only the law it sets itself: a state its income tax and Proposition 13, a rate
area its voter-approved debt rates. A parcel's situs is the leaf; everything above it applies.

The data files live in `augur/sim/data/jurisdictions/*.yaml`. The
loader resolves them relative to this module's location so Bazel's
runfiles tree (which preserves the source layout) can find them.
"""

from __future__ import annotations

from collections.abc import Set
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from finance.augur.sim.fixed_point import validate_currency_amount, validate_rate
from finance.augur.sim.ids import JurisdictionId


class StatutoryAmount(StrEnum):
    """A dollar amount in a jurisdiction's rules, which statute either indexes or fixes."""

    ORDINARY_INCOME_BRACKETS = "ordinary_income_brackets"
    LTCG_BRACKETS = "ltcg_brackets"
    STANDARD_DEDUCTION = "standard_deduction"
    MAX_CAPITAL_LOSS_ORDINARY_OFFSET = "max_capital_loss_ordinary_offset"
    NET_INVESTMENT_INCOME_TAX = "net_investment_income_tax"
    TAXABLE_INCOME_SURTAX = "taxable_income_surtax"


class BillRounding(StrEnum):
    """How a tax collector rounds a fiscal year's secured bill, which it collects in two halves."""

    # Each half rounded down to the quantum, so the bill is an even number of quanta.
    DOWN_TO_EVEN = "down_to_even"


class StatutoryIndexation(StrEnum):
    """Whether statute adjusts an amount for inflation each year or fixes it in nominal dollars."""

    CPI = "cpi"
    FIXED = "fixed"


_DATA_DIR = Path(__file__).parent / "data" / "jurisdictions"


def _validate_bracket_upper(value: object) -> Decimal | Literal["Infinity"]:
    if isinstance(value, float):
        raise TypeError("tax bracket upper must be an exact decimal string or integer")
    try:
        upper = value if isinstance(value, Decimal) else Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("tax bracket upper must be an exact decimal or Infinity") from exc
    if upper == Decimal("Infinity"):
        return "Infinity"
    if upper.is_nan() or upper < 0:
        raise ValueError("tax bracket upper must be nonnegative")
    return upper


type BracketUpper = Annotated[Decimal | Literal["Infinity"], BeforeValidator(_validate_bracket_upper)]
type CurrencyAmount = Annotated[Decimal, BeforeValidator(validate_currency_amount)]
type Rate = Annotated[Decimal, BeforeValidator(validate_rate)]


class TaxBracket(BaseModel):
    """One marginal-rate slice. `upper` is the inclusive upper
    edge; the exact string `Infinity` denotes the open-ended top
    bracket. Brackets in a schedule are walked low to high, and the
    rate applies to income in the slice
    `(previous_upper, upper]`."""

    model_config = ConfigDict(extra="forbid")

    upper: BracketUpper
    rate: Rate


class ThresholdTax(BaseModel):
    """A flat-rate additional tax on the part of some income measure above a threshold.

    Which measure a field applies it to is the field's contract; the threshold is keyed by
    filing status, and the jurisdiction's `indexation` says whether it is inflation-indexed.
    """

    model_config = ConfigDict(extra="forbid")

    rate: Rate
    threshold: dict[str, CurrencyAmount]


class InterestExemptions(BaseModel):
    """The interest characters a jurisdiction does not tax. `Taxable` interest has no entry: it
    is taxable everywhere by definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    treasury: bool = Field(description="Whether interest on Treasury obligations is exempt here.")
    municipal: Literal["all"] | Set[JurisdictionId] = Field(
        description="The states whose municipal obligations' interest is exempt here, or `all` for every state's."
    )


class IncomeTax(BaseModel):
    """A jurisdiction's income tax: brackets and deductions keyed by filing status.

    `ltcg_brackets` is optional: when absent, the engine taxes
    long-term capital gains at the ordinary-income rate
    (California-style)."""

    model_config = ConfigDict(extra="forbid")

    law_year: int = Field(description="The tax year whose statute and published inflation adjustments the amounts are.")
    indexation: dict[StatutoryAmount, StatutoryIndexation] = Field(
        description=(
            "Per dollar amount this jurisdiction carries, whether statute indexes it for inflation "
            "or fixes it; every amount present is tagged and nothing else is."
        )
    )
    ordinary_income_brackets: dict[str, list[TaxBracket]]
    ltcg_brackets: dict[str, list[TaxBracket]] | None = Field(default=None)
    standard_deduction: dict[str, CurrencyAmount]
    max_capital_loss_ordinary_offset: dict[str, CurrencyAmount] = Field(
        description=(
            "How much of a net capital loss may offset ordinary income in one year, by filing "
            "status; the rest carries forward. Federal IRC 1211(b) sets $3,000 and most states "
            "conform, but not all -- New Jersey allows none of it -- so it is stated per "
            "jurisdiction rather than assumed."
        )
    )
    exempt_interest: InterestExemptions
    net_investment_income_tax: ThresholdTax | None = Field(
        default=None,
        description=(
            "IRC 1411: `rate` times the lesser of net investment income and modified adjusted gross "
            "income above `threshold`. Absent where the jurisdiction levies no such tax."
        ),
    )
    taxable_income_surtax: ThresholdTax | None = Field(
        default=None,
        description=(
            "`rate` times taxable income above `threshold`, on top of the bracket tax (California's "
            "Behavioral Health Services Tax). Absent where the jurisdiction levies no such tax."
        ),
    )

    @model_validator(mode="after")
    def _every_amount_tagged(self) -> IncomeTax:
        present = {
            StatutoryAmount.ORDINARY_INCOME_BRACKETS,
            StatutoryAmount.STANDARD_DEDUCTION,
            StatutoryAmount.MAX_CAPITAL_LOSS_ORDINARY_OFFSET,
        }
        for amount, field in (
            (StatutoryAmount.LTCG_BRACKETS, self.ltcg_brackets),
            (StatutoryAmount.NET_INVESTMENT_INCOME_TAX, self.net_investment_income_tax),
            (StatutoryAmount.TAXABLE_INCOME_SURTAX, self.taxable_income_surtax),
        ):
            if field is not None:
                present.add(amount)
        if set(self.indexation) != present:
            raise ValueError(f"indexation tags {sorted(self.indexation)}, not the amounts {sorted(present)}")
        return self


class Proposition13(BaseModel):
    """California's ad-valorem limits (Cal. Const. art. XIII A) and the homeowners' exemption."""

    model_config = ConfigDict(extra="forbid")

    base_rate: Rate = Field(description="The general levy on assessed value, before any voter-approved debt rate.")
    inflation_cap: Rate = Field(description="The most a lien date may grow an assessed value by.")
    inflation_factors: dict[int, Rate] = Field(
        description=(
            "The Board of Equalization's published factor by lien-date year (the January 1 that opens "
            "fiscal year `year`-`year + 1`). A lien year after the last published one is simulated."
        )
    )
    homeowners_exemption: CurrencyAmount = Field(
        description="The reduction of taxable value for a home that is its owner's principal residence on the lien date."
    )
    supplemental_proration: dict[int, Rate] = Field(
        description=(
            "The share of a full year's tax a supplemental assessment on the current roll bears, by the "
            "month (1-12) of the first day after the change in ownership; a month absent bears none."
        )
    )


class TransferTaxBracket(BaseModel):
    """The rate a consideration pays once it reaches `lower`; `lower_included` says whether `lower` itself does."""

    model_config = ConfigDict(extra="forbid")

    lower: CurrencyAmount
    lower_included: bool
    rate: CurrencyAmount = Field(description="The tax on each `per` of the whole consideration, or part of one.")


class TransferTax(BaseModel):
    """A documentary or city transfer tax on the whole consideration at the rate of the highest bracket it
    reaches; below the lowest bracket nothing is due."""

    model_config = ConfigDict(extra="forbid")

    per: CurrencyAmount
    brackets: list[TransferTaxBracket] = Field(min_length=1)


class Jurisdiction(BaseModel):
    """One level of the tree: the law this level sets, and the level above it."""

    model_config = ConfigDict(extra="forbid")

    jurisdiction_id: JurisdictionId
    parent: Jurisdiction | None = Field(default=None, description="The level whose law also applies here.")
    income_tax: IncomeTax | None = Field(default=None, description="Absent where this level levies no income tax.")
    proposition_13: Proposition13 | None = Field(
        default=None, description="Absent below the state that sets the ad-valorem limits."
    )
    debt_rates: dict[int, Rate] | None = Field(
        default=None,
        description=(
            "A tax rate area's voter-approved debt rate on top of the base rate, by the year fiscal year "
            "`year`-`year + 1` starts. Absent above the rate area."
        ),
    )
    bill_rounding: BillRounding | None = Field(
        default=None,
        description=(
            "How the rate area's collector rounds a secured bill. Absent where no rule is published, and "
            "the bill rounds to the nearest quantum."
        ),
    )
    transfer_tax: TransferTax | None = Field(
        default=None,
        description="The tax this level levies on a transfer of real property; absent where it levies none.",
    )

    def lineage(self) -> tuple[Jurisdiction, ...]:
        """This level, then each level above it up to the root."""
        return (self,) if self.parent is None else (self, *self.parent.lineage())


def load_jurisdiction(jurisdiction_id: JurisdictionId) -> Jurisdiction:
    """Load and validate the YAML for `jurisdiction_id`, loading its parents in turn. Raises
    `FileNotFoundError` if a file is missing and Pydantic's `ValidationError` if the schema
    doesn't match."""
    path = _DATA_DIR / f"{jurisdiction_id}.yaml"
    data = yaml.safe_load(path.read_text())
    if "parent" in data:
        data["parent"] = load_jurisdiction(JurisdictionId(data["parent"]))
    return Jurisdiction.model_validate(data)
