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
from pydantic import BaseModel, BeforeValidator, Field

from finance.augur.sim.fixed_point import validate_currency_amount


class JurisdictionLevel(StrEnum):
    """Where a taxing authority sits. Load-bearing because exemptions are stated by level:
    "interest from any STATE issuer" is a rule federal law actually contains."""

    FEDERAL = "federal"
    STATE = "state"


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


class TaxBracket(BaseModel):
    """One marginal-rate slice. `upper` is the inclusive upper
    edge; the exact string `Infinity` denotes the open-ended top
    bracket. Brackets in a schedule are walked low to high, and the
    rate applies to income in the slice
    `(previous_upper, upper]`."""

    upper: BracketUpper
    rate: float


class Jurisdiction(BaseModel):
    """A taxing authority's complete bracket + deduction config.

    `ltcg_brackets` is optional: when absent, the engine taxes
    long-term capital gains at the ordinary-income rate
    (California-style)."""

    jurisdiction_id: str
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

    def taxes_interest_from(self, issuer_jurisdiction_id: str | None, issuer_level: JurisdictionLevel | None) -> bool:
        """Whether interest issued by `issuer_jurisdiction_id` is taxable HERE.

        `None` issuer means a non-governmental issuer (a corporate bond), which no jurisdiction
        exempts. "In-state" never appears as data — it is `issuer_jurisdiction_id == self`.
        """

        if issuer_jurisdiction_id is None or issuer_level is None:
            return True
        if issuer_jurisdiction_id == self.jurisdiction_id:
            return not self.exempts_own_issue
        return issuer_level not in self.exempt_interest_from_levels


def load_jurisdiction(jurisdiction_id: str) -> Jurisdiction:
    """Load and validate the YAML for `jurisdiction_id`. Raises
    `FileNotFoundError` if the file is missing and Pydantic's
    `ValidationError` if the schema doesn't match."""
    path = _DATA_DIR / f"{jurisdiction_id}.yaml"
    data = yaml.safe_load(path.read_text())
    return Jurisdiction.model_validate(data)
