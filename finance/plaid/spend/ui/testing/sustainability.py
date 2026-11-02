"""Synthetic sustainability responses for Spend visual tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from finance.plaid.spend.allowance import AllowancePolicy, AnalysisCategory, Kind, NamePrefix, Rule, Transaction
from finance.plaid.spend.portfolio_models import (
    PlaidSourcePolicy,
    PortfolioPolicy,
    PortfolioSources,
    PortfolioView,
    SourceValuation,
)
from finance.plaid.spend.reconciliation import ReportStatus
from finance.plaid.spend.reconciliation_views import sustainability_view
from finance.plaid.spend.sustainability import (
    AccountHistory,
    AccountOpening,
    DocumentedHistory,
    SpendingAccountCoverage,
    SustainabilityDashboard,
    SustainabilityDisplay,
    SustainabilityHistoryPoint,
    SustainabilityPolicy,
    SustainabilityView,
    WithdrawalTax,
    calculate_sustainability,
)

_THIRTY_DAY_START = date(2026, 9, 15)
_THIRTY_DAY_END = date(2026, 10, 14)
_THIRTY_DAY_HISTORY_START = _THIRTY_DAY_START - timedelta(days=4)
_SYNTHETIC_SPENDING_ACCOUNT = "synthetic-spending"

type Variant = Literal["ready", "stale", "unavailable", "provisional", "tax_unmodeled", "low_draw"]


def view(variant: Variant = "ready", *, window_days: int = 90) -> SustainabilityView:
    if variant not in {"ready", "stale", "unavailable", "provisional", "tax_unmodeled", "low_draw"}:
        raise ValueError(f"Unknown sustainability variant: {variant}")
    stale = variant == "stale"
    unavailable = variant == "unavailable"
    is_provisional = variant == "provisional"
    tax_unmodeled = variant == "tax_unmodeled"
    low_draw = variant == "low_draw"
    portfolio_unavailable = stale or unavailable
    portfolio_net = None if portfolio_unavailable else 125_000_000
    annualized_spending = 600_621 if low_draw else 4_951_065
    effective_tax_rate = None if tax_unmodeled else Decimal("0.12")

    def rates_for(annualized: int) -> tuple[Decimal | None, Decimal | None]:
        if portfolio_net is None:
            return None, None
        if effective_tax_rate is None:
            return Decimal(annualized) / Decimal(portfolio_net), None
        tax_adjusted = int((Decimal(annualized) / (Decimal(1) - effective_tax_rate)).quantize(Decimal(1)))
        return Decimal(annualized) / Decimal(portfolio_net), Decimal(tax_adjusted) / Decimal(portfolio_net)

    history: list[SustainabilityHistoryPoint] = []
    history_values = (
        (570_000, 585_000, annualized_spending) if low_draw else (4_700_000, 4_850_000, annualized_spending)
    )
    for day, annualized in zip(("2026-10-12", "2026-10-13", "2026-10-14"), history_values, strict=True):
        spending_rate, tax_adjusted_rate = rates_for(annualized)
        if is_provisional and day == "2026-10-14":
            point_status = ReportStatus.PROVISIONAL
        elif portfolio_unavailable:
            point_status = ReportStatus.UNAVAILABLE
        else:
            point_status = ReportStatus.READY
        history.append(
            SustainabilityHistoryPoint(
                date=date.fromisoformat(day),
                status=point_status,
                covered_days=90,
                annualized_spending_minor_units=annualized,
                spending_draw_rate=spending_rate,
                tax_adjusted_draw_rate=tax_adjusted_rate,
            )
        )

    spending_draw_rate, tax_adjusted_draw_rate = rates_for(annualized_spending)
    estimated_annual_draw = (
        None
        if portfolio_unavailable or effective_tax_rate is None
        else int((Decimal(annualized_spending) / (Decimal(1) - effective_tax_rate)).quantize(Decimal(1)))
    )
    mandatory, flexible, provisional, total = (
        (92_000, 50_000, 6_000, 148_000) if low_draw else (760_000, 410_000, 50_000, 1_220_000)
    )
    if is_provisional:
        view_status = ReportStatus.PROVISIONAL
    elif portfolio_unavailable:
        view_status = ReportStatus.UNAVAILABLE
    else:
        view_status = ReportStatus.READY
    if is_provisional:
        issues = ["Review the unverified reversal and confirm its classification before relying on this estimate."]
    elif stale or unavailable:
        issues = ["Synthetic source has delayed updates."]
    else:
        issues = []
    portfolio = PortfolioView(
        status="unavailable" if portfolio_unavailable else "ready",
        net_minor_units=portfolio_net,
        assets_minor_units=None if portfolio_unavailable else 145_000_000,
        liabilities_minor_units=None if portfolio_unavailable else 15_000_000,
        excluded_minor_units=None if portfolio_unavailable else 5_000_000,
        sources=[
            SourceValuation(
                name="Synthetic investment source",
                status="unavailable" if portfolio_unavailable else "ready",
                amount_minor_units=None if portfolio_unavailable else 125_000_000,
                as_of=datetime(2026, 8, 30, 12, tzinfo=UTC) if stale else datetime(2026, 10, 14, 12, tzinfo=UTC),
                issues=["Synthetic valuation has not refreshed recently."] if stale else [],
            ),
            SourceValuation(
                name="Synthetic cash source",
                status="unavailable" if unavailable else "ready",
                amount_minor_units=None if unavailable else 20_000_000,
                as_of=datetime(2026, 10, 14, 12, tzinfo=UTC),
                issues=[],
            ),
        ],
    )
    tax_assumption_note = (
        None
        if tax_unmodeled
        else "Synthetic fixture tax assumption; actual tax treatment depends on account and income details."
    )
    return SustainabilityView(
        generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
        status=view_status,
        currency="USD",
        time_zone="UTC",
        window_days=window_days,
        available_windows_days=sorted({30, 90, window_days}),
        period_start=date(2026, 7, 17),
        period_end=date(2026, 10, 14),
        covered_days=90,
        mandatory_minor_units=mandatory,
        flexible_minor_units=flexible,
        provisional_minor_units=provisional,
        spending_minor_units=total,
        annualized_spending_minor_units=annualized_spending,
        estimated_annual_draw_minor_units=estimated_annual_draw,
        spending_draw_rate=spending_draw_rate,
        tax_adjusted_draw_rate=tax_adjusted_draw_rate,
        effective_tax_rate=effective_tax_rate,
        tax_assumption_note=tax_assumption_note,
        tax_assumption_as_of=None if tax_unmodeled else date(2026, 1, 1),
        reference_draw_rates=[Decimal("0.03"), Decimal("0.035"), Decimal("0.04"), Decimal("0.045")],
        portfolio=portfolio,
        history=history,
        account_coverage=[],
        issues=issues,
        known_issues=[
            "Synthetic fixture: investment values are illustrative and can lag market changes.",
            "No income outside observed spending and portfolio sources is represented.",
        ],
    )


def with_history(
    view: SustainabilityView, history: list[SustainabilityHistoryPoint], *, issues: list[str] | None = None
) -> SustainabilityView:
    return view.model_copy(update={"history": history, "issues": view.issues if issues is None else issues})


def dashboard(*windows: SustainabilityView) -> SustainabilityDashboard:
    return SustainabilityDashboard(default_window_days=90, windows=list(windows))


def _calculated_thirty_day_view() -> SustainabilityView:
    """Build the 30-day visual series from policy inputs and a repeating ledger."""
    time_zone = ZoneInfo("UTC")
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    totals_by_kind = {Kind.FIXED: 200_000, Kind.FLEXIBLE: 100_000, Kind.REVIEW: 10_000}
    names = {
        Kind.FIXED: "SYNTHETIC FIXED PURCHASE",
        Kind.FLEXIBLE: "SYNTHETIC FLEXIBLE PURCHASE",
        Kind.REVIEW: "SYNTHETIC REVIEW PURCHASE",
    }
    transactions: list[Transaction] = []
    for offset in range((_THIRTY_DAY_END - _THIRTY_DAY_HISTORY_START).days + 1):
        day = _THIRTY_DAY_HISTORY_START + timedelta(days=offset)
        repeated_day = offset % 30
        for kind, total in totals_by_kind.items():
            quotient, remainder = divmod(total, 30)
            amount_minor_units = quotient + int(repeated_day < remainder)
            transactions.append(
                Transaction(
                    account_id=_SYNTHETIC_SPENDING_ACCOUNT,
                    transaction_id=f"{kind.value}-{day.isoformat()}",
                    pending_transaction_id=None,
                    date=day,
                    amount=Decimal(amount_minor_units) / 100,
                    pending=False,
                    name=names[kind],
                    merchant_name=None,
                    pfc_primary=None,
                    pfc_detailed=None,
                    currency="USD",
                )
            )

    allowance = AllowancePolicy(
        monthly_minor_units=70_000,
        activation_at=_THIRTY_DAY_HISTORY_START,
        spending_account_ids={_SYNTHETIC_SPENDING_ACCOUNT},
        rules=[
            Rule(condition=NamePrefix(field="name", prefix="SYNTHETIC FIXED"), kind=Kind.FIXED),
            Rule(condition=NamePrefix(field="name", prefix="SYNTHETIC FLEXIBLE"), kind=Kind.FLEXIBLE),
            Rule(condition=NamePrefix(field="name", prefix="SYNTHETIC REVIEW"), kind=Kind.REVIEW),
        ],
        analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#777777")},
    )
    policy = SustainabilityPolicy(
        display=SustainabilityDisplay(
            windows_days=[30, 90, 365],
            default_window_days=90,
            reference_draw_rates=[Decimal("0.03"), Decimal("0.035"), Decimal("0.04"), Decimal("0.045")],
        ),
        portfolio=PortfolioPolicy(
            sources=PortfolioSources(
                plaid=PlaidSourcePolicy(asset_account_ids=["synthetic-investments", "synthetic-cash"])
            )
        ),
        withdrawal_tax=WithdrawalTax(
            rate=Decimal("0.12"),
            assumption_note="Synthetic fixture tax assumption; actual tax treatment depends on account and income details.",
            as_of=date(2026, 1, 1),
        ),
        account_history={
            _SYNTHETIC_SPENDING_ACCOUNT: AccountHistory(
                coverage=DocumentedHistory(
                    complete_from=_THIRTY_DAY_HISTORY_START,
                    source="Synthetic sustainability ledger",
                    as_of=_THIRTY_DAY_END,
                )
            )
        },
        known_issues=[
            "Synthetic fixture: investment values are illustrative and can lag market changes.",
            "No income outside observed spending and portfolio sources is represented.",
        ],
    )
    portfolio = PortfolioView(
        status="ready",
        net_minor_units=125_000_000,
        assets_minor_units=145_000_000,
        liabilities_minor_units=15_000_000,
        excluded_minor_units=5_000_000,
        sources=[
            SourceValuation(
                name="Synthetic investment source",
                status="ready",
                amount_minor_units=125_000_000,
                as_of=datetime(2026, 10, 14, 12, tzinfo=UTC),
            ),
            SourceValuation(
                name="Synthetic cash source",
                status="ready",
                amount_minor_units=20_000_000,
                as_of=datetime(2026, 10, 14, 12, tzinfo=UTC),
            ),
        ],
    )
    return sustainability_view(
        calculate_sustainability(
            policy,
            allowance,
            transactions,
            portfolio,
            time_zone=time_zone,
            now=now,
            window_days=30,
            source_issues=[],
            account_labels={_SYNTHETIC_SPENDING_ACCOUNT: "Example checking"},
        )
    )


def window(days: int, *, history: list[SustainabilityHistoryPoint] | None = None) -> SustainabilityView:
    if days not in {30, 90, 365}:
        raise ValueError(f"Unsupported sustainability window: {days}")
    if days == 30:
        result = _calculated_thirty_day_view()
        return result if history is None else with_history(result, history)
    result = view()
    if days == 90:
        return result if history is None else with_history(result, history)
    mandatory, flexible, provisional = 3_000_000, 2_000_000, 300_000
    total = mandatory + flexible + provisional
    annual = int((Decimal(total) * Decimal("365.2425") / days).quantize(Decimal(1)))
    funding = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    dates = ("2026-10-13", "2026-10-14")
    history_points = (
        history
        if history is not None
        else [
            SustainabilityHistoryPoint(
                date=date.fromisoformat(day),
                status=ReportStatus.READY,
                covered_days=days,
                annualized_spending_minor_units=annual,
                spending_draw_rate=Decimal(annual) / Decimal(125_000_000),
                tax_adjusted_draw_rate=Decimal(funding) / Decimal(125_000_000),
            )
            for day in dates
        ]
    )
    return result.model_copy(
        update={
            "window_days": days,
            "available_windows_days": [30, 90, 365],
            "period_start": date(2026, 10, 15) - timedelta(days=days),
            "covered_days": days,
            "mandatory_minor_units": mandatory,
            "flexible_minor_units": flexible,
            "provisional_minor_units": provisional,
            "spending_minor_units": total,
            "annualized_spending_minor_units": annual,
            "estimated_annual_draw_minor_units": funding,
            "spending_draw_rate": Decimal(annual) / Decimal(125_000_000),
            "tax_adjusted_draw_rate": Decimal(funding) / Decimal(125_000_000),
            "history": history_points,
        }
    )


def partial_window() -> SustainabilityView:
    result = view()
    return result.model_copy(
        update={
            "period_start": date(2026, 8, 31),
            "covered_days": 45,
            "mandatory_minor_units": 380_000,
            "flexible_minor_units": 205_000,
            "provisional_minor_units": 25_000,
            "spending_minor_units": 610_000,
            "history": [],
        }
    )


def with_partial_account_history() -> SustainabilityView:
    result = view()
    annual = int((Decimal(610_000) * Decimal("365.2425") / 44).quantize(Decimal(1)))
    draw = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    account_coverage = [
        SpendingAccountCoverage(
            account_label="Example checking",
            first_recorded_on=date(2026, 9, 1),
            opening=AccountOpening(
                earliest=date(2026, 7, 20),
                latest=date(2026, 7, 27),
                source="Synthetic account archive",
                as_of=date(2026, 10, 14),
            ),
            coverage=None,
            unknown_days_in_window=43,
        ),
        SpendingAccountCoverage(
            account_label="Example card",
            first_recorded_on=date(2026, 8, 15),
            opening=None,
            coverage=DocumentedHistory(
                complete_from=date(2026, 8, 15), source="Synthetic statement archive", as_of=date(2026, 10, 14)
            ),
            unknown_days_in_window=29,
        ),
    ]
    return result.model_copy(
        update={
            "period_start": date(2026, 9, 1),
            "covered_days": 44,
            "mandatory_minor_units": 380_000,
            "flexible_minor_units": 205_000,
            "provisional_minor_units": 25_000,
            "spending_minor_units": 610_000,
            "annualized_spending_minor_units": annual,
            "estimated_annual_draw_minor_units": draw,
            "spending_draw_rate": Decimal(annual) / Decimal(125_000_000),
            "tax_adjusted_draw_rate": Decimal(draw) / Decimal(125_000_000),
            "history": [],
            "account_coverage": account_coverage,
        }
    )


def with_historical_coverage_gap() -> SustainabilityView:
    result = view()
    history = [
        SustainabilityHistoryPoint(
            date=date(2026, 10, 8),
            status=ReportStatus.READY,
            covered_days=90,
            annualized_spending_minor_units=4_700_000,
            spending_draw_rate=Decimal("0.0376"),
            tax_adjusted_draw_rate=Decimal("0.0427272727"),
        ),
        SustainabilityHistoryPoint(
            date=date(2026, 10, 9),
            status=ReportStatus.READY,
            covered_days=90,
            annualized_spending_minor_units=4_850_000,
            spending_draw_rate=Decimal("0.0388"),
            tax_adjusted_draw_rate=Decimal("0.0440909091"),
        ),
        *[
            SustainabilityHistoryPoint(
                date=date(2026, 10, day),
                status=ReportStatus.UNAVAILABLE,
                covered_days=89 - (day - 10),
                annualized_spending_minor_units=None,
                spending_draw_rate=None,
                tax_adjusted_draw_rate=None,
            )
            for day in range(10, 15)
        ],
    ]
    return result.model_copy(
        update={
            "status": ReportStatus.UNAVAILABLE,
            "period_start": date(2026, 7, 17),
            "covered_days": 0,
            "mandatory_minor_units": 0,
            "flexible_minor_units": 0,
            "provisional_minor_units": 0,
            "spending_minor_units": 0,
            "annualized_spending_minor_units": None,
            "estimated_annual_draw_minor_units": None,
            "spending_draw_rate": None,
            "tax_adjusted_draw_rate": None,
            "history": history,
            "account_coverage": [
                SpendingAccountCoverage(
                    account_label="Example new account",
                    first_recorded_on=None,
                    opening=AccountOpening(
                        earliest=date(2026, 10, 10),
                        latest=date(2026, 10, 14),
                        source="Synthetic opening archive",
                        as_of=date(2026, 10, 14),
                    ),
                    coverage=None,
                    unknown_days_in_window=5,
                )
            ],
        }
    )
