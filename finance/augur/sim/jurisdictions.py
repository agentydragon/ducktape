"""Tax jurisdiction definitions loaded from YAML.

A jurisdiction is one taxing authority — federal U.S., California
state, etc. Each carries bracket schedules (ordinary income;
optionally a separate LTCG schedule) and a standard deduction
keyed by filing status.

The data files live in `augur/sim/data/jurisdictions/*.yaml`. The
loader resolves them relative to this module's location so Bazel's
runfiles tree (which preserves the source layout) can find them.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Field, model_validator

from finance.augur.sim.fixed_point import validate_currency_amount, validate_rate
from finance.augur.sim.ids import JurisdictionId


class JurisdictionLevel(StrEnum):
    """Where a taxing authority sits. Load-bearing because exemptions are stated by level:
    "interest from any STATE issuer" is a rule federal law actually contains."""

    FEDERAL = "federal"
    STATE = "state"


class StatutoryAmount(StrEnum):
    """A dollar amount in a jurisdiction's rules, which statute either indexes or fixes."""

    ORDINARY_INCOME_BRACKETS = "ordinary_income_brackets"
    LTCG_BRACKETS = "ltcg_brackets"
    STANDARD_DEDUCTION = "standard_deduction"
    MAX_CAPITAL_LOSS_ORDINARY_OFFSET = "max_capital_loss_ordinary_offset"
    NET_INVESTMENT_INCOME_TAX = "net_investment_income_tax"
    TAXABLE_INCOME_SURTAX = "taxable_income_surtax"


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

    upper: BracketUpper
    rate: Rate


class ThresholdTax(BaseModel):
    """A flat-rate additional tax on the part of some income measure above a threshold.

    Which measure a field applies it to is the field's contract; the threshold is keyed by
    filing status, and the jurisdiction's `indexation` says whether it is inflation-indexed.
    """

    rate: Rate
    threshold: dict[str, CurrencyAmount]


class Jurisdiction(BaseModel):
    """A taxing authority's complete bracket + deduction config.

    `ltcg_brackets` is optional: when absent, the engine taxes
    long-term capital gains at the ordinary-income rate
    (California-style)."""

    jurisdiction_id: JurisdictionId
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
    level: JurisdictionLevel
    exempt_interest_from_levels: frozenset[JurisdictionLevel] = Field(
        default=frozenset(),
        description=(
            "Issuer LEVELS whose interest this jurisdiction does not tax. Federal exempts "
            "interest from any state issuer (IRC 103); a state exempts interest from federal "
            "obligations (31 USC 3124)."
        ),
    )
    exempts_own_issue: bool = Field(
        default=False,
        description=(
            "Whether this jurisdiction exempts interest on debt IT issued — the honest form of "
            '"in-state muni". California exempts California munis; the federal government does '
            "NOT exempt Treasuries."
        ),
    )
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
    def _every_amount_tagged(self) -> Jurisdiction:
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
            raise ValueError(
                f"{self.jurisdiction_id!r} indexation tags {sorted(self.indexation)}, not its amounts {sorted(present)}"
            )
        return self

    def taxes_interest_from(
        self, issuer_jurisdiction_id: JurisdictionId | None, issuer_level: JurisdictionLevel | None
    ) -> bool:
        """Whether interest issued by `issuer_jurisdiction_id` is taxable HERE.

        `None` issuer means a non-governmental issuer (a corporate bond), which no jurisdiction
        exempts. "In-state" never appears as data — it is `issuer_jurisdiction_id == self`.
        """

        if issuer_jurisdiction_id is None or issuer_level is None:
            return True
        if issuer_jurisdiction_id == self.jurisdiction_id:
            return not self.exempts_own_issue
        return issuer_level not in self.exempt_interest_from_levels


def load_jurisdiction(jurisdiction_id: JurisdictionId) -> Jurisdiction:
    """Load and validate the YAML for `jurisdiction_id`. Raises
    `FileNotFoundError` if the file is missing and Pydantic's
    `ValidationError` if the schema doesn't match."""
    path = _DATA_DIR / f"{jurisdiction_id}.yaml"
    data = yaml.safe_load(path.read_text())
    return Jurisdiction.model_validate(data)
