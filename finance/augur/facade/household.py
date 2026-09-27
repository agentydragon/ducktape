"""What a household brings to a run: its portfolio, dated flows, tax, first month and declared scope.

Every part is required and none has a default, so a household cannot inherit a decision it never made.
"""

from __future__ import annotations

from calendar import Month
from collections.abc import Set
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints, field_validator, model_validator

from finance.augur.facade.holdings import PortfolioConfig
from finance.augur.sim.ids import JurisdictionId
from finance.augur.sim.scenario import FilingStatus, NonNegativeCurrencyAmount, PositiveCurrencyAmount

type _NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CalendarMonth(_Declaration):
    year: int
    month: Month

    def months_since(self, origin: CalendarMonth) -> int:
        """This month's index in a run whose month 0 is `origin`."""
        return (self.year - origin.year) * 12 + self.month - origin.month

    def __str__(self) -> str:
        return f"{self.month.name.title()} {self.year}"


class OpenEnded(_Declaration):
    """A flow with no last month: it lasts as long as the run."""


class Nominal(_Declaration):
    """The amount stays as declared while prices move."""

    kind: Literal["nominal"] = "nominal"


class CpiIndexed(_Declaration):
    """The amount is in the start month's money and follows CPI from there."""

    kind: Literal["cpi_indexed"] = "cpi_indexed"
    adjustment_period_months: PositiveInt = Field(
        description="How often the amount is re-set to CPI, counted from month 0: 12 moves it each January."
    )


class FlowCategory(StrEnum):
    """What a flow is to the household, which fixes whether it pays in or out."""

    # Ordinary income paid to the household, such as wages or a pension; a taxed household owes tax on it.
    INCOME = "income"
    # A bill the household must pay, such as rent or insurance.
    OBLIGATION = "obligation"
    # Consumption the household intends; its policy may spend less.
    SPENDING = "spending"


class DatedFlow(_Declaration):
    """Money paid to or by the household in every month from `first` through `last`."""

    label: _NonBlank
    category: FlowCategory
    monthly_amount: PositiveCurrencyAmount
    indexation: Annotated[Nominal | CpiIndexed, Field(discriminator="kind")]
    first: CalendarMonth
    last: CalendarMonth | OpenEnded

    @model_validator(mode="after")
    def _ends_after_it_begins(self) -> DatedFlow:
        if isinstance(self.last, CalendarMonth) and self.last.months_since(self.first) < 0:
            raise ValueError(f"flow {self.label!r} ends in {self.last}, before it begins in {self.first}")
        return self


class FilingProfile(_Declaration):
    filing_status: FilingStatus
    jurisdiction_ids: Set[JurisdictionId] = Field(
        min_length=1,
        description=(
            "Every authority that taxes the household, such as `federal_us`. A household no authority taxes "
            "is `Untaxed`."
        ),
    )


class Taxed(_Declaration):
    kind: Literal["taxed"] = "taxed"
    filing: FilingProfile
    prior_year_tax: NonNegativeCurrencyAmount = Field(
        description=(
            "Last year's total tax. Each quarterly estimated payment is a quarter of it; zero means none, "
            "so the whole year's tax falls due at the January true-up."
        )
    )


class Untaxed(_Declaration):
    """The household is modelled without tax; `reason` says why."""

    kind: Literal["untaxed"] = "untaxed"
    reason: _NonBlank


class BiasDirection(StrEnum):
    """Which way leaving something out of the portfolio moves a run's results, in level or in spread."""

    # Results overstate the household, as when a debt is left out.
    OPTIMISTIC = "optimistic"
    # Results understate it, as when an asset is counted at zero.
    PESSIMISTIC = "pessimistic"
    # Outcomes spread wider than they would, as when a hedge is left out.
    WIDER = "wider"
    # Outcomes spread narrower than they would, as when a volatile holding is counted at a fixed value.
    NARROWER = "narrower"
    UNKNOWN = "unknown"


class Excluded(_Declaration):
    """Something the household has, owes or expects that its portfolio deliberately leaves out."""

    label: _NonBlank
    reason: _NonBlank
    direction: BiasDirection


class ReservedLiability(_Declaration):
    """A liability the portfolio does not carry, and how much of the portfolio is set aside to pay it."""

    label: _NonBlank
    earmarked: PositiveCurrencyAmount
    reason: _NonBlank


class DeclaredScope(_Declaration):
    """The household's balance sheet beyond its portfolio, which a run cannot see for itself.

    Empty `excluded` and `reserved` declare the portfolio to be the whole balance sheet.
    """

    excluded: tuple[Excluded, ...]
    reserved: tuple[ReservedLiability, ...]


class Household(_Declaration):
    """A household at month 0. A flow already running by then is declared from `start`."""

    portfolio: PortfolioConfig
    flows: tuple[DatedFlow, ...]
    tax: Annotated[Taxed | Untaxed, Field(discriminator="kind")]
    start: CalendarMonth = Field(
        description=(
            "The calendar month that is month 0. Only a January for now: a run's tax years begin at month 0, "
            "and a mid-year start would need the income, gains and payments of the year so far."
        )
    )
    scope: DeclaredScope

    @field_validator("start")
    @classmethod
    def _starts_in_january(cls, start: CalendarMonth) -> CalendarMonth:
        if start.month != Month.JANUARY:
            raise ValueError(f"a household cannot start in {start}: mid-year starts are not supported yet")
        return start

    @model_validator(mode="after")
    def _flows_begin_in_the_run(self) -> Household:
        early = [flow.label for flow in self.flows if flow.first.months_since(self.start) < 0]
        if early:
            raise ValueError(f"flows {early} begin before the household starts in {self.start}")
        return self
