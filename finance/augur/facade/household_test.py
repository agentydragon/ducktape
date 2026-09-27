from __future__ import annotations

from calendar import Month
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel
from pydantic import BaseModel, ValidationError

from finance.augur.facade.holdings import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.facade.household import (
    BiasDirection,
    CalendarMonth,
    CpiIndexed,
    DatedFlow,
    DeclaredScope,
    Excluded,
    FilingProfile,
    FlowCategory,
    Household,
    Nominal,
    OpenEnded,
    ReservedLiability,
    Taxed,
    Untaxed,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LotId
from finance.augur.sim.scenario import FilingStatus

START = CalendarMonth(year=2030, month=Month.JANUARY)
BROKERAGE = AccountId("test_brokerage")
PORTFOLIO = PortfolioConfig(
    accounts=(PortfolioAccountConfig(account_id=BROKERAGE, owner_agent_id=AgentId("test_household")),),
    holdings=(
        SecurityHoldingConfig(
            position_id="test_growth_position",
            account_id=BROKERAGE,
            symbol=SecuritySymbol("test-growth"),
            unit_value=Decimal(100),
            lots=(
                HoldingTaxLotConfig(
                    lot_id=LotId("test_growth_lot"),
                    holding_period_months_at_start=24,
                    quantity=100.0,
                    cost_basis=Decimal(8_000),
                ),
            ),
        ),
    ),
)
MONTHLY_CPI = CpiIndexed(adjustment_period_months=1)
SALARY = DatedFlow(
    label="test salary",
    category=FlowCategory.INCOME,
    monthly_amount=Decimal(5_000),
    indexation=Nominal(),
    first=START,
    last=CalendarMonth(year=2034, month=Month.DECEMBER),
)
LIVING_COSTS = DatedFlow(
    label="test living costs",
    category=FlowCategory.SPENDING,
    monthly_amount=Decimal(3_000),
    indexation=MONTHLY_CPI,
    first=START,
    last=OpenEnded(),
)
RENT = DatedFlow(
    label="test rent",
    category=FlowCategory.OBLIGATION,
    monthly_amount=Decimal(1_000),
    indexation=CpiIndexed(adjustment_period_months=12),
    first=CalendarMonth(year=2031, month=Month.JULY),
    last=OpenEnded(),
)
# First and last month coincide: a one-off payment.
FEE = DatedFlow(
    label="test one-off fee",
    category=FlowCategory.OBLIGATION,
    monthly_amount=Decimal(2_000),
    indexation=Nominal(),
    first=CalendarMonth(year=2032, month=Month.SEPTEMBER),
    last=CalendarMonth(year=2032, month=Month.SEPTEMBER),
)
FILING = FilingProfile(filing_status=FilingStatus.SINGLE, jurisdiction_ids={JurisdictionId("test_federal")})
# Zero is a declared answer: no estimated payments, the whole year's tax at the January true-up.
TAXED = Taxed(filing=FILING, prior_year_tax=Decimal(0))
UNTAXED = Untaxed(reason="test household holds only tax-free accounts")
EXCLUDED = Excluded(
    label="test private holding", reason="no market price: counted at zero", direction=BiasDirection.CONSERVATIVE
)
RESERVED = ReservedLiability(label="test tax already owed", earmarked=Decimal(10_000), reason="test bills held for it")
SCOPE = DeclaredScope(excluded=(EXCLUDED,), reserved=(RESERVED,))
HOUSEHOLD = Household(portfolio=PORTFOLIO, flows=(SALARY, LIVING_COSTS, RENT, FEE), tax=TAXED, start=START, scope=SCOPE)

# Every field of every declaration, bar a union's variant tag, is a decision without a default.
DECISIONS = [
    (declaration, field)
    for declaration in (HOUSEHOLD, START, SALARY, MONTHLY_CPI, FILING, TAXED, UNTAXED, SCOPE, EXCLUDED, RESERVED)
    for field in type(declaration).model_fields
    if field != "kind"
]
FREE_TEXT = [
    (SALARY, "label"),
    (UNTAXED, "reason"),
    (EXCLUDED, "label"),
    (EXCLUDED, "reason"),
    (RESERVED, "label"),
    (RESERVED, "reason"),
]


def _ids(cases: Sequence[tuple[BaseModel, str]]) -> list[str]:
    return [f"{type(declaration).__name__}.{field}" for declaration, field in cases]


def _rejected_fields(declaration: type[BaseModel], given: dict[str, Any]) -> set[int | str]:
    """The fields `declaration` refuses in `given`: untyped input, as a notebook or an agent passes it."""
    with pytest.raises(ValidationError) as rejected:
        declaration.model_validate(given)
    return {error["loc"][0] for error in rejected.value.errors()}


def test_a_household_places_its_flows_in_run_months() -> None:
    assert [flow.first.months_since(HOUSEHOLD.start) for flow in HOUSEHOLD.flows] == [0, 0, 18, 32]


def test_empty_scope_declares_the_portfolio_the_whole_balance_sheet() -> None:
    Household(portfolio=PORTFOLIO, flows=(), tax=UNTAXED, start=START, scope=DeclaredScope(excluded=(), reserved=()))


@pytest.mark.parametrize(("declaration", "field"), DECISIONS, ids=_ids(DECISIONS))
def test_a_decision_cannot_be_left_out_or_none(declaration: BaseModel, field: str) -> None:
    others = {name: value for name, value in declaration if name != field}
    assert _rejected_fields(type(declaration), others) == {field}
    assert _rejected_fields(type(declaration), {**others, field: None}) == {field}


@pytest.mark.parametrize(("declaration", "field"), FREE_TEXT, ids=_ids(FREE_TEXT))
def test_free_text_must_say_something(declaration: BaseModel, field: str) -> None:
    assert _rejected_fields(type(declaration), {**dict(declaration), field: "  "}) == {field}


def test_bias_direction_is_a_closed_choice() -> None:
    assert _rejected_fields(Excluded, {**dict(EXCLUDED), "direction": "pessimistic"}) == {"direction"}


def test_a_taxed_household_files_with_some_authority() -> None:
    with pytest.raises(ValidationError, match="jurisdiction_ids"):
        FilingProfile(filing_status=FilingStatus.SINGLE, jurisdiction_ids=set())


@pytest.mark.parametrize(
    "month", [month for month in Month if month != Month.JANUARY], ids=lambda month: month.name.title()
)
def test_a_mid_year_start_is_refused_by_name(month: Month) -> None:
    with pytest.raises(
        ValidationError, match=f"cannot start in {month.name.title()} 2030: mid-year starts are not supported yet"
    ):
        Household(portfolio=PORTFOLIO, flows=(), tax=TAXED, start=CalendarMonth(year=2030, month=month), scope=SCOPE)


def test_a_flow_cannot_begin_before_the_household() -> None:
    running = DatedFlow(
        label="test salary",
        category=FlowCategory.INCOME,
        monthly_amount=Decimal(5_000),
        indexation=Nominal(),
        first=CalendarMonth(year=2029, month=Month.DECEMBER),
        last=OpenEnded(),
    )
    with pytest.raises(ValidationError, match=r"\['test salary'\] begin before the household starts in January 2030"):
        Household(portfolio=PORTFOLIO, flows=(running,), tax=TAXED, start=START, scope=SCOPE)


def test_a_flow_cannot_end_before_it_begins() -> None:
    with pytest.raises(ValidationError, match="ends in June 2030, before it begins in July 2030"):
        DatedFlow(
            label="test salary",
            category=FlowCategory.INCOME,
            monthly_amount=Decimal(5_000),
            indexation=Nominal(),
            first=CalendarMonth(year=2030, month=Month.JULY),
            last=CalendarMonth(year=2030, month=Month.JUNE),
        )


if __name__ == "__main__":
    pytest_bazel.main()
