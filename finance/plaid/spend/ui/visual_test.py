"""Render the decision-first Spend page with synthetic allowance data.

Screenshot artifacts are published for PR review; this does not exercise private
login or assert that a deployed authenticated browser looks the same.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Locator, Page, Playwright, Route, expect

from finance.plaid.spend.allowance import (
    AllOf,
    AllowanceSpendPeriod,
    AllowanceView,
    AmountExact,
    AmountSign,
    AnalysisCategory,
    AnyOf,
    DateRange,
    Disposition,
    EstimatePeriodId,
    FieldExact,
    ForecastView,
    Kind,
    NameContains,
    NamePrefix,
    PaceAlert,
    Period,
    PeriodId,
    PlaidCounterparty,
    RecordedPacePeriod,
    ReversalPair,
    Rule,
    RuleScope,
    Status,
    TransactionPeriodId,
    UnmatchedCharges,
)
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    AnalysisCategoryView,
    CardConfigurationView,
    CardView,
    ExcludedFlowSummary,
    PaceEffect,
    PlaidPaymentMeta,
    PlaidPersonalFinanceCategory,
    PlaidTransactionDetails,
    PlaidTransactionLocation,
    ProvisionalCardPeriod,
    SpendConfigurationView,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementCycle,
    StatementReason,
    TransactionPeriodSummary,
)
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual import deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage, playwright_trace

# pytest_plugins loads util.playwright by name; Gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)

_UI_DIR = get_required_path("_main/finance/plaid/spend/ui/dist/index.html").parent


_FROZEN_NOW_MS = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp() * 1000)
_TRANSACTION_VIEWPORTS = [(1280, 960), (390, 844), (320, 720)]


@pytest.fixture
async def page(playwright: Playwright, request: pytest.FixtureRequest) -> AsyncIterator[Page]:
    capture_name = request.node.name.removeprefix("test_")
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 1280, "height": 960}, frozen_now_ms=_FROZEN_NOW_MS
        ) as context,
        playwright_trace(context, undeclared_outputs_dir(), capture_name),
    ):
        yield await context.new_page()


@pytest.fixture
async def touch_page(playwright: Playwright, request: pytest.FixtureRequest) -> AsyncIterator[Page]:
    capture_name = request.node.name.removeprefix("test_")
    async with (
        deterministic_browser_context(
            playwright, viewport={"width": 390, "height": 844}, frozen_now_ms=_FROZEN_NOW_MS, has_touch=True
        ) as context,
        playwright_trace(context, undeclared_outputs_dir(), capture_name),
    ):
        yield await context.new_page()


@pytest.fixture
def view(page: Page) -> VisualPage:
    return VisualPage(page, output_dir=undeclared_outputs_dir(), title="Spend")


async def _expand_accordion(control: Locator) -> None:
    await control.click()
    await expect(control).to_have_attribute("aria-expanded", "true")
    panel_id = await control.get_attribute("aria-controls")
    assert panel_id is not None
    # Wait for this panel's full content height, not unrelated page animations.
    await control.page.wait_for_function(
        """id => {
            const panel = document.getElementById(id);
            if (!panel || panel.getBoundingClientRect().height === 0) return false;
            const expanding = panel.getAnimations().some(animation =>
                animation instanceof CSSTransition && animation.transitionProperty === "height"
                && animation.playState !== "finished");
            return !expanding && panel.scrollHeight <= panel.clientHeight + 1;
        }""",
        arg=panel_id,
    )


def _example_sustainability(variant: str = "ready") -> dict[str, object]:
    stale = variant == "stale"
    unavailable = variant == "unavailable"
    tax_unmodeled = variant == "tax_unmodeled"
    low_draw = variant == "low_draw"
    portfolio_unavailable = stale or unavailable
    portfolio_net = None if portfolio_unavailable else 125_000_000
    annualized_spending = 600_621 if low_draw else 4_951_065
    effective_tax_rate = None if tax_unmodeled else 0.12

    def rates_for(annualized: int) -> tuple[float | None, float | None]:
        if portfolio_net is None:
            return None, None
        if effective_tax_rate is None:
            return annualized / portfolio_net, None
        tax_adjusted = int((Decimal(annualized) / (Decimal(1) - Decimal(str(effective_tax_rate)))).quantize(Decimal(1)))
        return annualized / portfolio_net, tax_adjusted / portfolio_net

    history = []
    history_values = (
        (570_000, 585_000, annualized_spending) if low_draw else (4_700_000, 4_850_000, annualized_spending)
    )
    for day, annualized in zip(("2026-10-12", "2026-10-13", "2026-10-14"), history_values, strict=True):
        spending_rate, tax_adjusted_rate = rates_for(annualized)
        history.append(
            {
                "date": day,
                "covered_days": 90,
                "annualized_spending_minor_units": annualized,
                "spending_draw_rate": spending_rate,
                "tax_adjusted_draw_rate": tax_adjusted_rate,
            }
        )

    spending_draw_rate, tax_adjusted_draw_rate = rates_for(annualized_spending)
    estimated_annual_draw = (
        None
        if portfolio_unavailable or effective_tax_rate is None
        else int((Decimal(annualized_spending) / (Decimal(1) - Decimal(str(effective_tax_rate)))).quantize(Decimal(1)))
    )
    mandatory, flexible, provisional, total = (
        (92_000, 50_000, 6_000, 148_000) if low_draw else (760_000, 410_000, 50_000, 1_220_000)
    )
    return {
        "generated_at": "2026-10-15T12:00:00Z",
        "status": "unavailable" if portfolio_unavailable else "ready",
        "currency": "USD",
        "time_zone": "UTC",
        "window_days": 90,
        "available_windows_days": [30, 90],
        "period_start": "2026-07-17",
        "period_end": "2026-10-14",
        "covered_days": 90,
        "mandatory_minor_units": mandatory,
        "flexible_minor_units": flexible,
        "provisional_minor_units": provisional,
        "spending_minor_units": total,
        "annualized_spending_minor_units": annualized_spending,
        "estimated_annual_draw_minor_units": estimated_annual_draw,
        "spending_draw_rate": spending_draw_rate,
        "tax_adjusted_draw_rate": tax_adjusted_draw_rate,
        "effective_tax_rate": effective_tax_rate,
        "tax_assumption_note": (
            None
            if tax_unmodeled
            else "Synthetic fixture tax assumption; actual tax treatment depends on account and income details."
        ),
        "tax_assumption_as_of": None if tax_unmodeled else "2026-01-01",
        "reference_draw_rates": [0.03, 0.035, 0.04, 0.045],
        "portfolio": {
            "status": "unavailable" if portfolio_unavailable else "ready",
            "net_minor_units": portfolio_net,
            "assets_minor_units": None if portfolio_unavailable else 145_000_000,
            "liabilities_minor_units": None if portfolio_unavailable else 15_000_000,
            "excluded_minor_units": None if portfolio_unavailable else 5_000_000,
            "sources": [
                {
                    "name": "Synthetic investment source",
                    "status": "unavailable" if portfolio_unavailable else "ready",
                    "amount_minor_units": None if portfolio_unavailable else 125_000_000,
                    "as_of": "2026-08-30T12:00:00Z" if stale else "2026-10-14T12:00:00Z",
                    "issues": ["Synthetic valuation has not refreshed recently."] if stale else [],
                },
                {
                    "name": "Synthetic cash source",
                    "status": "unavailable" if unavailable else "ready",
                    "amount_minor_units": None if unavailable else 20_000_000,
                    "as_of": "2026-10-14T12:00:00Z",
                    "issues": [],
                },
            ],
        },
        "history": history,
        "account_coverage": [],
        "issues": ["Synthetic source has delayed updates."] if stale or unavailable else [],
        "known_issues": [
            "Synthetic fixture: investment values are illustrative and can lag market changes.",
            "No income outside observed spending and portfolio sources is represented.",
        ],
    }


def _sustainability_dashboard(*windows: dict[str, object]) -> dict[str, object]:
    return {"default_window_days": 90, "windows": list(windows)}


def _example_sustainability_window(days: int) -> dict[str, object]:
    result = _example_sustainability()
    if days == 90:
        return result
    mandatory, flexible, provisional = (200_000, 100_000, 10_000) if days == 30 else (3_000_000, 2_000_000, 300_000)
    total = mandatory + flexible + provisional
    annual = int((Decimal(total) * Decimal("365.2425") / days).quantize(Decimal(1)))
    funding = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    dates = (
        ("2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14")
        if days == 30
        else ("2026-10-13", "2026-10-14")
    )
    result.update(
        window_days=days,
        covered_days=days,
        available_windows_days=[30, 90, 365],
        period_start=(date(2026, 10, 15) - timedelta(days=days)).isoformat(),
        mandatory_minor_units=mandatory,
        flexible_minor_units=flexible,
        provisional_minor_units=provisional,
        spending_minor_units=total,
        annualized_spending_minor_units=annual,
        estimated_annual_draw_minor_units=funding,
        spending_draw_rate=annual / 125_000_000,
        tax_adjusted_draw_rate=funding / 125_000_000,
        history=[
            {
                "date": day,
                "covered_days": days,
                "annualized_spending_minor_units": annual,
                "spending_draw_rate": annual / 125_000_000,
                "tax_adjusted_draw_rate": funding / 125_000_000,
            }
            for day in dates
        ],
    )
    return result


def _example_sustainability_with_partial_account_history() -> dict[str, object]:
    result = _example_sustainability()
    annual = int((Decimal(610_000) * Decimal("365.2425") / 44).quantize(Decimal(1)))
    draw = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    result.update(
        mandatory_minor_units=380_000,
        flexible_minor_units=205_000,
        provisional_minor_units=25_000,
        spending_minor_units=610_000,
        annualized_spending_minor_units=annual,
        estimated_annual_draw_minor_units=draw,
        spending_draw_rate=annual / 125_000_000,
        tax_adjusted_draw_rate=draw / 125_000_000,
    )
    result["covered_days"] = 44
    result["period_start"] = "2026-09-01"
    result["history"] = []
    result["account_coverage"] = [
        {
            "account_label": "Example checking",
            "first_recorded_on": "2026-09-01",
            "opening": {
                "earliest": "2026-07-20",
                "latest": "2026-07-27",
                "source": "Synthetic account archive",
                "as_of": "2026-10-14",
            },
            "coverage": None,
            "unknown_days_in_window": 43,
        },
        {
            "account_label": "Example card",
            "first_recorded_on": "2026-08-15",
            "opening": None,
            "coverage": {"complete_from": "2026-08-15", "source": "Synthetic statement archive", "as_of": "2026-10-14"},
            "unknown_days_in_window": 29,
        },
    ]
    return result


def _example_sustainability_with_historical_coverage_gap() -> dict[str, object]:
    result = _example_sustainability()
    result.update(
        status="unavailable",
        covered_days=0,
        period_start="2026-07-17",
        mandatory_minor_units=0,
        flexible_minor_units=0,
        provisional_minor_units=0,
        spending_minor_units=0,
        annualized_spending_minor_units=None,
        estimated_annual_draw_minor_units=None,
        spending_draw_rate=None,
        tax_adjusted_draw_rate=None,
    )
    result["account_coverage"] = [
        {
            "account_label": "Example new account",
            "first_recorded_on": None,
            "opening": {
                "earliest": "2026-10-10",
                "latest": "2026-10-14",
                "source": "Synthetic opening archive",
                "as_of": "2026-10-14",
            },
            "coverage": None,
            "unknown_days_in_window": 5,
        }
    ]
    result["history"] = [
        {
            "date": "2026-10-08",
            "covered_days": 90,
            "annualized_spending_minor_units": 4_700_000,
            "spending_draw_rate": 0.0376,
            "tax_adjusted_draw_rate": 0.0427272727,
        },
        {
            "date": "2026-10-09",
            "covered_days": 90,
            "annualized_spending_minor_units": 4_850_000,
            "spending_draw_rate": 0.0388,
            "tax_adjusted_draw_rate": 0.0440909091,
        },
        *[
            {
                "date": f"2026-10-{day:02}",
                "covered_days": 89 - (day - 10),
                "annualized_spending_minor_units": None,
                "spending_draw_rate": None,
                "tax_adjusted_draw_rate": None,
            }
            for day in range(10, 15)
        ],
    ]
    return result


async def _select_spend_page(page: Page, label: str) -> None:
    navigation_button = page.get_by_role("button", name="Open page navigation")
    if await navigation_button.is_visible():
        await navigation_button.click()
        await page.get_by_role("menuitem", name=label).click()
    else:
        await page.get_by_role("tab", name=label).click()


async def _expect_tooltip_rendered(tooltip: Locator) -> None:
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_have_css("opacity", "1")
    fits_viewport = await tooltip.evaluate(
        """(element) => {
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.left >= 0 &&
            rect.bottom <= window.innerHeight && rect.right <= window.innerWidth;
        }"""
    )
    assert fits_viewport, "tooltip is clipped by the viewport"


async def _expect_tooltip_hidden(tooltip: Locator) -> None:
    await expect(tooltip).to_be_hidden()


@pytest.fixture(scope="module")
def dashboard_url() -> Iterator[str]:
    app = FastAPI()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_UI_DIR / "index.html")

    def example_full_reversal_pair() -> ReversalPair:
        return ReversalPair(
            id="example-full-reversal",
            description="Synthetic example of a fully reversed purchase.",
            original=AllOf(
                conditions=[
                    NameContains(field="name", substring="EXAMPLE OWN ACCOUNT TRANSFER"),
                    AmountExact(value="120.00"),
                    DateRange(start=date(2026, 10, 11), end=date(2026, 10, 11)),
                ]
            ),
            reversal=AllOf(
                conditions=[
                    NameContains(field="name", substring="EXAMPLE OWN ACCOUNT REVERSAL"),
                    AmountExact(value="-120.00"),
                    DateRange(start=date(2026, 10, 12), end=date(2026, 10, 12)),
                ]
            ),
        )

    def example_unresolved_reversal_pair() -> ReversalPair:
        return ReversalPair(
            id="example-reversal-needs-review",
            description="Synthetic selector with no matching original transaction.",
            original=AllOf(
                conditions=[
                    NameContains(field="name", substring="EXAMPLE MISSING ORIGINAL"),
                    AmountExact(value="15.50"),
                    DateRange(start=date(2026, 10, 7), end=date(2026, 10, 7)),
                ]
            ),
            reversal=AllOf(
                conditions=[
                    NameContains(field="name", substring="EXAMPLE MISSING REVERSAL"),
                    AmountExact(value="-15.50"),
                    DateRange(start=date(2026, 10, 8), end=date(2026, 10, 8)),
                ]
            ),
        )

    def example_allowance(warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None) -> AllowanceView:
        today = date(2026, 10, 15)
        estimate_period_id = estimate_period_id or PeriodId.ROLLING_7D
        activation = today if warmup else date(2026, 10, 1)
        spend_amounts = {
            PeriodId.CREDIT_CYCLE: 55000,
            PeriodId.CALENDAR_MONTH: 55000,
            PeriodId.YEAR_TO_DATE: 55000,
            PeriodId.ROLLING_7D: 8750,
            PeriodId.ROLLING_30D: 50000,
        }
        spend_periods = []
        for period_id, amount in spend_amounts.items():
            period = Period.for_id(period_id, today, activation)
            spend_periods.append(
                AllowanceSpendPeriod(
                    period=period, counted_from=max(period.start, activation), spend_minor_units=0 if warmup else amount
                )
            )
        return AllowanceView(
            status=Status.ACTIVE,
            time_zone="UTC",
            currency="USD",
            monthly_minor_units=70000,
            activation_at=activation,
            available_minor_units=70000 if warmup else 20000,
            next_credit_at=datetime(2026, 10, 25, tzinfo=UTC),
            posted_minor_units=0 if warmup else 52500,
            pending_minor_units=0 if warmup else 2500,
            review_minor_units=0 if warmup else 1500,
            review_transaction_count=0 if warmup else 2,
            unmatched_refunds_minor_units=0,
            spend_periods=spend_periods,
            recorded_pace_periods=[
                RecordedPacePeriod(
                    period=Period.for_id(period_id, today),
                    observed_daily_minor_units=None if warmup else observed,
                    unmatched_charges=UnmatchedCharges(
                        count=0 if warmup else 2, amount_minor_units=0 if warmup else 300
                    ),
                )
                for period_id, observed in ((PeriodId.ROLLING_7D, 1250), (PeriodId.ROLLING_30D, 1000))
            ],
            forecast=ForecastView(
                basis_period=Period.for_id(estimate_period_id, today),
                daily_pace_minor_units=None
                if warmup
                else (1250 if estimate_period_id == PeriodId.ROLLING_7D else 1000),
                projected_cycle_end_minor_units=None
                if warmup
                else (7500 if estimate_period_id == PeriodId.ROLLING_7D else 10000),
                estimated_exhaustion_at=None if warmup else datetime(2026, 10, 31, tzinfo=UTC),
                alert_state=PaceAlert.UNAVAILABLE if warmup else PaceAlert.NORMAL,
            ),
            spending_signal=PaceAlert.UNAVAILABLE if warmup else PaceAlert.NORMAL,
            last_synced_at=datetime(2026, 10, 15, 11, tzinfo=UTC),
            prior_carry_minor_units=0 if warmup else 5000,
            note="Reversal pair example-reversal-needs-review: "
            "Original selector matched 0 retained transactions; exactly one is required.",
        )

    @app.get("/api/v1/view", response_model=SpendView)
    def view(warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None) -> SpendView:
        synced = datetime(2026, 10, 15, 11, tzinfo=UTC)
        return SpendView(
            generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
            allowance=example_allowance(warmup, estimate_period_id),
            cards=[
                CardView(
                    account_id="synthetic-card",
                    label="Example card",
                    account_name="Example card",
                    mask="0000",
                    institution_name="Sample Bank",
                    currency="USD",
                    statement_period=StatementCycle(start=date(2026, 10, 1), through=date(2026, 10, 15)),
                    alert_state=AlertState.NORMAL,
                    alert_threshold_percent=None,
                    spend_minor_units=22000,
                    limit_minor_units=100000,
                    spend_percent=22,
                    pending_minor_units=1000,
                    posted_minor_units=21000,
                    last_synced_at=synced,
                ),
                CardView(
                    account_id="synthetic-new-card",
                    label="New example card",
                    account_name="New example card",
                    mask="1111",
                    institution_name="Sample Credit Union",
                    currency="USD",
                    statement_period=ProvisionalCardPeriod(start=date(2026, 10, 10), through=date(2026, 10, 15)),
                    alert_state=AlertState.UNAVAILABLE,
                    alert_threshold_percent=None,
                    spend_minor_units=3900,
                    limit_minor_units=100000,
                    spend_percent=None,
                    pending_minor_units=0,
                    posted_minor_units=3900,
                    last_synced_at=synced,
                ),
            ],
        )

    @app.get("/api/v1/events")
    async def events(estimate_period_id: EstimatePeriodId | None = None, warmup: bool = False) -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            yield (
                f"event: view\ndata: "
                f"{json.dumps(view(warmup=warmup, estimate_period_id=estimate_period_id).model_dump(mode='json'))}\n\n"
            )
            while True:
                await asyncio.sleep(60)
                yield ": keepalive\n\n"

        return StreamingResponse(updates(), media_type="text/event-stream")

    @app.get("/api/v1/configuration", response_model=SpendConfigurationView)
    def configuration() -> SpendConfigurationView:
        return SpendConfigurationView(
            time_zone="UTC",
            cards=[
                CardConfigurationView(
                    label="Example card", enabled=True, limit_minor_units=None, alert_threshold_percent=None
                )
            ],
            allowance=AllowanceConfigurationView(
                monthly_minor_units=70000,
                activation_at=date(2026, 10, 1),
                currency="USD",
                spending_account_count=1,
                max_sync_age_hours=72,
                forecast_basis_period_id=PeriodId.ROLLING_7D,
                rules=[
                    Rule(
                        condition=AllOf(
                            conditions=[
                                NameContains(field="name", substring="EXAMPLE FUNDING"),
                                DateRange(start=date(2026, 10, 3), end=date(2026, 10, 3)),
                            ]
                        ),
                        kind=Kind.EXCLUDED,
                        scope=RuleScope.ONE_OFF,
                        id="example-funding-transfer",
                        description="Own-account funding leg, confirmed by the owner 2026-10-04.",
                    ),
                    Rule(
                        condition=AllOf(
                            conditions=[
                                AmountSign(sign="negative"),
                                AnyOf(
                                    conditions=[
                                        NamePrefix(field="name", prefix="EXAMPLE"),
                                        FieldExact(field="merchant_category_code", value="5812"),
                                    ]
                                ),
                            ]
                        ),
                        kind=Kind.REVIEW,
                        analysis_category="refund_review",
                        description="Unverified credit; inspect the earlier purchase before netting it.",
                    ),
                ],
                analysis_categories={
                    "unclassified": AnalysisCategory(label="Unclassified", color="#D97706"),
                    "refund_review": AnalysisCategory(label="Refund review", color="#B45309"),
                    "shipping": AnalysisCategory(label="Document shipping", color="#0F766E"),
                    "travel": AnalysisCategory(label="Holiday travel", color="#7C3AED"),
                },
                reversal_pairs=[example_full_reversal_pair(), example_unresolved_reversal_pair()],
            ),
        )

    @app.get("/api/v1/transactions", response_model=SpendTransactionsView)
    def transactions(period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
        today = date(2026, 10, 15)
        rows = [
            SpendTransactionRow(
                date=date(2026, 10, 15),
                account_label="Example card",
                name="EXAMPLE CAFE PURCHASE",
                merchant_name="Example Cafe",
                amount_minor_units=1500,
                currency="USD",
                pending=True,
                allowance_in_scope=True,
                disposition=Disposition.COUNTED,
                rule_number=None,
                rule=None,
                allowance_minor_units=1500,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=1500)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=1500,
                statement_reason=StatementReason.COUNTED,
                pfc_primary="FOOD_AND_DRINK",
                pfc_detailed="FOOD_AND_DRINK_COFFEE",
                merchant_category_code="5812",
                category=AnalysisCategoryView(id="unclassified", label="Unclassified", color="#D97706"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 14),
                account_label="Example card",
                name="UPS SHIPPING",
                merchant_name="UPS",
                amount_minor_units=1850,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.FIXED,
                rule_number=2,
                effective_kind=Kind.FIXED,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="UPS"),
                    kind=Kind.FIXED,
                    analysis_category="shipping",
                    description="Required document shipping for a synthetic example.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=1850,
                statement_reason=StatementReason.COUNTED,
                pfc_primary="TRANSPORTATION",
                pfc_detailed="TRANSPORTATION_SHIPPING",
                merchant_category_code="4215",
                category=AnalysisCategoryView(id="shipping", label="Document shipping", color="#0F766E"),
                counterparties=[
                    PlaidCounterparty(
                        name="Example Shipping",
                        type="merchant",
                        confidence_level="HIGH",
                        entity_id="synthetic-merchant",
                        website="https://example.invalid/shipping",
                    ),
                    PlaidCounterparty(name="Example Marketplace", type="marketplace"),
                ],
                details=PlaidTransactionDetails(
                    account_id="synthetic-account",
                    transaction_id="synthetic-transaction",
                    amount=Decimal("18.50"),
                    iso_currency_code="USD",
                    original_description="EXAMPLE SHIPPING PAYMENT",
                    authorized_date=date(2026, 10, 13),
                    payment_channel="in store",
                    location=PlaidTransactionLocation(city="Example City", region="CA", country="US"),
                    payment_meta=PlaidPaymentMeta(reference_number="synthetic-reference"),
                    personal_finance_category=PlaidPersonalFinanceCategory(
                        primary="TRANSPORTATION", detailed="TRANSPORTATION_SHIPPING", confidence_level="HIGH"
                    ),
                ),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 13),
                account_label="Example card",
                name="EXAMPLE REFUND",
                merchant_name=None,
                amount_minor_units=-4200,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.HELD_REFUND,
                rule_number=1,
                effective_kind=Kind.REVIEW,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE"),
                    kind=Kind.REVIEW,
                    description="Confirm the purchase before netting this refund.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=-4200,
                statement_reason=StatementReason.COUNTED,
                pfc_primary="GENERAL_MERCHANDISE",
                pfc_detailed="GENERAL_MERCHANDISE_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="unclassified", label="Unclassified", color="#D97706"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 11),
                account_label="Example checking",
                name="EXAMPLE OWN ACCOUNT TRANSFER",
                merchant_name=None,
                amount_minor_units=12_000,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.REVERSED,
                rule_number=3,
                effective_kind=Kind.EXCLUDED,
                reversal_pair=example_full_reversal_pair(),
                rule=Rule(
                    condition=AllOf(
                        conditions=[
                            NameContains(field="name", substring="EXAMPLE OWN ACCOUNT TRANSFER"),
                            DateRange(start=date(2026, 10, 11), end=date(2026, 10, 11)),
                        ]
                    ),
                    kind=Kind.EXCLUDED,
                    scope=RuleScope.ONE_OFF,
                    id="example-own-account-leg",
                    description="Second leg of one own-account transfer, confirmed by the owner 2026-10-12.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TRANSFER_OUT",
                pfc_detailed="TRANSFER_OUT_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 12),
                account_label="Example checking",
                name="EXAMPLE OWN ACCOUNT REVERSAL",
                merchant_name=None,
                amount_minor_units=-12_000,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.REVERSED,
                rule_number=2,
                effective_kind=Kind.EXCLUDED,
                reversal_pair=example_full_reversal_pair(),
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE OWN ACCOUNT"),
                    kind=Kind.REVIEW,
                    description="Synthetic rule classification retained as context for the reversal.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TRANSFER_IN",
                pfc_detailed="TRANSFER_IN_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 2),
                account_label="Example checking",
                name="EXAMPLE TRAVEL PURCHASE",
                merchant_name="Example Travel",
                amount_minor_units=53500,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.COUNTED,
                rule_number=3,
                effective_kind=Kind.FLEXIBLE,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE TRAVEL"),
                    kind=Kind.FLEXIBLE,
                    analysis_category="travel",
                    description="Synthetic discretionary trip purchase.",
                ),
                allowance_minor_units=53500,
                pace_effects=[
                    PaceEffect(
                        period_id=period_id, amount_minor_units=53500 if period_id == PeriodId.ROLLING_30D else 0
                    )
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TRAVEL",
                pfc_detailed="TRAVEL_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="travel", label="Holiday travel", color="#7C3AED"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 10),
                account_label="Example checking",
                name="EXAMPLE OWN TRANSFER DEBIT",
                merchant_name=None,
                amount_minor_units=12_500,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.EXCLUDED,
                effective_kind=Kind.EXCLUDED,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE OWN TRANSFER"),
                    kind=Kind.EXCLUDED,
                    analysis_category="own_transfers",
                    description="Synthetic own-account transfer excluded from the allowance.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TRANSFER_OUT",
                pfc_detailed="TRANSFER_OUT_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 11),
                account_label="Example checking",
                name="EXAMPLE OWN TRANSFER CREDIT",
                merchant_name=None,
                amount_minor_units=-12_500,
                currency="USD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.EXCLUDED,
                effective_kind=Kind.EXCLUDED,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE OWN TRANSFER"),
                    kind=Kind.EXCLUDED,
                    analysis_category="own_transfers",
                    description="Synthetic own-account transfer excluded from the allowance.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TRANSFER_IN",
                pfc_detailed="TRANSFER_IN_OTHER",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 12),
                account_label="Example checking",
                name="EXAMPLE TAX PAYMENT",
                merchant_name=None,
                amount_minor_units=41_500,
                currency="USD",
                pending=True,
                allowance_in_scope=True,
                disposition=Disposition.EXCLUDED,
                effective_kind=Kind.EXCLUDED,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE TAX"),
                    kind=Kind.EXCLUDED,
                    analysis_category="taxes",
                    description="Synthetic tax payment accounted for separately.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TAXES",
                pfc_detailed="TAXES_OTHER_TAXES",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
            ),
            SpendTransactionRow(
                date=date(2026, 10, 13),
                account_label="Example checking",
                name="EXAMPLE TAX PAYMENT CAD",
                merchant_name=None,
                amount_minor_units=7_200,
                currency="CAD",
                pending=False,
                allowance_in_scope=True,
                disposition=Disposition.EXCLUDED,
                effective_kind=Kind.EXCLUDED,
                rule=Rule(
                    condition=NamePrefix(field="name", prefix="EXAMPLE TAX"),
                    kind=Kind.EXCLUDED,
                    analysis_category="taxes",
                    description="Synthetic foreign-currency tax payment accounted for separately.",
                ),
                allowance_minor_units=0,
                pace_effects=[
                    PaceEffect(period_id=period_id, amount_minor_units=0)
                    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
                ],
                statement_minor_units=None,
                statement_reason=None,
                pfc_primary="TAXES",
                pfc_detailed="TAXES_OTHER_TAXES",
                merchant_category_code=None,
                category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
            ),
        ]
        shown = [row for row in rows if row.date >= PeriodId(period).start(today, date(2026, 10, 1))]
        return SpendTransactionsView(
            generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
            requested_period_id=period,
            period=Period.for_id(PeriodId(period), today, date(2026, 10, 1)),
            summary=TransactionPeriodSummary(
                transaction_count=len(shown),
                net_allowance_spend_minor_units=sum(row.allowance_minor_units for row in shown),
                unmatched_charge_count=1,
                unmatched_charge_minor_units=1500,
                excluded_flows=[
                    ExcludedFlowSummary(
                        category=AnalysisCategoryView(
                            id="own_transfers", label="Own-account transfers", color="#64748B"
                        ),
                        currency="USD",
                        debit_minor_units=12_500,
                        credit_minor_units=12_500,
                        transaction_count=2,
                        pending_count=0,
                    ),
                    ExcludedFlowSummary(
                        category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
                        currency="USD",
                        debit_minor_units=41_500,
                        credit_minor_units=0,
                        transaction_count=1,
                        pending_count=1,
                    ),
                    ExcludedFlowSummary(
                        category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
                        currency="CAD",
                        debit_minor_units=7_200,
                        credit_minor_units=0,
                        transaction_count=1,
                        pending_count=0,
                    ),
                ],
            ),
            allowance=example_allowance(),
            rows=shown,
        )

    @app.get("/api/v1/sustainability/windows")
    def sustainability() -> dict[str, object]:
        return _sustainability_dashboard(*(_example_sustainability_window(days) for days in (30, 90, 365)))

    app.mount("/static", StaticFiles(directory=_UI_DIR))
    with serve_app_sync(app) as url:
        yield url


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [390, 1024, 1280])
async def test_header_menu_at_right_with_sign_out(page: Page, dashboard_url: str, width: int) -> None:
    await page.set_viewport_size({"width": width, "height": 844})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    menu_button = page.get_by_role("button", name="Open page navigation")
    await expect(menu_button).to_be_visible()
    container = page.locator("header .mantine-Container-root")
    container_bounds = await container.bounding_box()
    button_bounds = await menu_button.bounding_box()
    assert container_bounds is not None
    assert button_bounds is not None
    assert abs(button_bounds["x"] + button_bounds["width"] - container_bounds["x"] - container_bounds["width"]) < 20
    await expect(page.get_by_role("button", name="Sign out")).to_have_count(0)
    await menu_button.click()
    sign_out = page.get_by_role("menuitem", name="Sign out")
    await expect(sign_out).to_be_visible()
    assert await sign_out.locator("xpath=ancestor::form").get_attribute("action") == "/auth/logout"
    assert await sign_out.locator("xpath=ancestor::form").get_attribute("method") == "post"


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_spending_decision_render(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})

    async def serve_pre_activation_history(route: Route) -> None:
        response = await route.fetch()
        transactions = await response.json()
        if transactions["requested_period_id"] == "rolling_30d":
            historical = next(row for row in transactions["rows"] if row["name"] == "EXAMPLE TRAVEL PURCHASE")
            historical["date"] = "2026-09-30"
            historical["disposition"] = "pace_only"
            historical["allowance_minor_units"] = 0
        await route.fulfill(response=response, json=transactions)

    await page.route("**/api/v1/transactions*", serve_pre_activation_history)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.locator("canvas[aria-label]").wait_for()
    await page.get_by_text("$200", exact=True).wait_for()
    await expect(page.get_by_role("heading", name="Flexible spending", level=1)).to_have_count(1)
    await expect(page.get_by_text("Earlier purchases inform this graph", exact=False)).to_be_hidden()
    await page.get_by_text("How this chart works", exact=True).click()
    await expect(page.get_by_text("Earlier purchases inform this graph", exact=False)).to_be_visible()
    await page.get_by_text("How this chart works", exact=True).click()
    assert await page.evaluate(
        """() => {
            const chart = document.getElementById("spending-history-title");
            const allowance = document.getElementById("allowance-title");
            return Boolean(
                chart && allowance &&
                (chart.compareDocumentPosition(allowance) & Node.DOCUMENT_POSITION_FOLLOWING)
            );
        }"""
    )
    await expect(page.get_by_role("alert").get_by_text("2 charges ($15) need review")).to_have_count(1)
    await expect(page.get_by_role("heading", name="Can I afford this?")).to_have_count(1)
    pair_review = page.get_by_role("alert").filter(has_text="Allowance needs review")
    await expect(pair_review.get_by_text("example-reversal-needs-review", exact=False)).to_be_visible()
    await expect(
        pair_review.get_by_text("Original selector matched 0 retained transactions", exact=False)
    ).to_be_visible()
    await expect(page.get_by_text("7 days", exact=True)).to_have_count(1)
    await expect(page.locator('span[title="$12.50"]').first).to_be_attached()
    await expect(page.locator('span[title="$200.00"]').first).to_be_attached()
    await expect(page.get_by_text("30 days", exact=True)).to_have_count(1)
    await expect(page.get_by_text("$13 / day", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Below provisional leash", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Unmatched in selected window: 2 ($3)", exact=False)).to_have_count(1)
    await expect(page.get_by_text("$75", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Provisional card total since", exact=False)).to_have_count(1)
    await expect(page.get_by_text("Includes purchases outside the allowance", exact=False)).to_have_count(1)
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    normal = f"dashboard-{width}.png"
    await view.capture(normal.removesuffix(".png"), label=f"{width}px available", full_page=True, animations="disabled")
    await page.get_by_text("30 days", exact=True).click()
    await expect(page.locator("#spending-history-summary")).to_have_text(
        "$550 flexible spending over 30 days; average $18 per day; leash reference $23 per day."
    )
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    thirty_days = f"dashboard-{width}-30-days.png"
    await view.capture(
        thirty_days.removesuffix(".png"),
        label=f"{width}px 30-day category chart",
        full_page=True,
        animations="disabled",
    )
    await page.get_by_text("7 days", exact=True).click()
    await expect(page.locator("#spending-history-summary")).to_have_text(
        "$15 flexible spending over 7 days; average $2 per day; leash reference $23 per day."
    )
    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    await expect(page.get_by_text("-$50", exact=True)).to_have_count(1)
    await expect(page.get_by_text("$175 short before", exact=False)).to_have_count(1)
    view.errors.assert_none(context="Spend")
    await page.get_by_label("Hypothetical flexible purchase").fill("200.01")
    await expect(page.get_by_text("-<$1", exact=True)).to_have_count(1)
    await expect(page.locator('span[title="-$0.01"]')).to_have_count(1)
    exceeded = f"dashboard-{width}-purchase.png"
    await view.capture(
        exceeded.removesuffix(".png"), label=f"{width}px hypothetical purchase", full_page=True, animations="disabled"
    )


@pytest.mark.asyncio
async def test_transactions_refresh_on_initial_event_without_view_get(page: Page, dashboard_url: str) -> None:
    release_events = asyncio.Event()
    events_requested = asyncio.Event()
    view_requests: list[str] = []
    transaction_requests: list[str] = []

    async def delayed_events(route: Route) -> None:
        events_requested.set()
        await release_events.wait()
        await route.continue_()

    async def reject_view(route: Route) -> None:
        view_requests.append(route.request.url)
        await route.abort()

    await page.route("**/api/v1/view*", reject_view)
    await page.route("**/api/v1/events*", delayed_events)
    page.on(
        "request",
        lambda request: transaction_requests.append(request.url) if "/api/v1/transactions?" in request.url else None,
    )
    try:
        async with page.expect_request("**/api/v1/transactions?period=rolling_7d"):
            await page.goto(dashboard_url, wait_until="domcontentloaded")
        await events_requested.wait()
        assert view_requests == []
        async with page.expect_request("**/api/v1/transactions?period=rolling_7d"):
            release_events.set()
        await page.locator("canvas[aria-label]").wait_for()
        assert len(transaction_requests) == 2
        assert view_requests == []
    finally:
        release_events.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_spending_decision_dark_theme(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.emulate_media(color_scheme="dark")
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Where you stand").wait_for()
    await expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", "dark")
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    initial = f"dashboard-dark-{width}.png"
    await view.capture(
        initial.removesuffix(".png"), label=f"{width}px dark theme", full_page=True, animations="disabled"
    )
    await page.get_by_label("Hypothetical flexible purchase").fill("100")
    await page.get_by_text("Pace warning", exact=True).last.wait_for()
    view.errors.assert_none(context="Spend")
    warning = f"dashboard-dark-{width}-warning.png"
    await view.capture(
        warning.removesuffix(".png"), label=f"{width}px dark pace warning", full_page=True, animations="disabled"
    )
    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    view.errors.assert_none(context="Spend")
    exceeded = f"dashboard-dark-{width}-exceeded.png"
    await view.capture(
        exceeded.removesuffix(".png"), label=f"{width}px dark over allowance", full_page=True, animations="disabled"
    )


@pytest.mark.asyncio
async def test_new_allowance_has_no_fake_zero_pace(page: Page, view: VisualPage, dashboard_url: str) -> None:
    async def serve_warmup(route: Route) -> None:
        await route.continue_(url=f"{dashboard_url}/api/v1/events?warmup=true")

    await page.route("**/api/v1/events*", serve_warmup)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_text("Not enough data", exact=True).wait_for()
    await expect(page.get_by_text("Pace warming up", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Warming up", exact=True)).to_have_count(1)
    await expect(page.get_by_text("$700", exact=True).first).to_be_attached()
    await page.get_by_label("Hypothetical flexible purchase").fill("10")
    await expect(page.get_by_text("$690", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Pace estimate warming up", exact=False)).to_have_count(1)
    image = "dashboard-warmup.png"
    await view.capture(
        image.removesuffix(".png"), label="New allowance warming up", full_page=True, animations="disabled"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_review_rule_configuration_render(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Configuration")
    rule_notes = page.locator("details").filter(
        has_text="Unverified credit; inspect the earlier purchase before netting it."
    )
    await expect(rule_notes.locator("summary")).to_be_visible()
    await rule_notes.locator("summary").click()
    await page.get_by_text("Unverified credit; inspect the earlier purchase before netting it.").wait_for()
    await expect(page.get_by_text("Review", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Amount is negative AND (Transaction name starts with", exact=False)).to_have_count(1)
    await expect(page.get_by_text("One-off", exact=True)).to_have_count(1)
    await page.get_by_text("example-funding-transfer", exact=True).wait_for()
    await expect(
        page.get_by_text("Own-account funding leg, confirmed by the owner 2026-10-04.", exact=True)
    ).to_have_count(1)
    await expect(page.get_by_text("Date is 2026-10-03", exact=False)).to_have_count(1)
    await page.get_by_text("Full reversal pairs", exact=True).wait_for()
    await page.get_by_text("example-full-reversal", exact=True).wait_for()
    await page.get_by_text("example-reversal-needs-review", exact=True).wait_for()
    await expect(page.get_by_text("Original debit", exact=True)).to_have_count(2)
    await expect(page.get_by_text("Reversal credit", exact=True)).to_have_count(2)
    view.errors.assert_none(context="Spend")
    image = f"configuration-review-{width}.png"
    await view.capture(
        image.removesuffix(".png"), label=f"{width}px review rule", full_page=True, animations="disabled"
    )


async def _open_transaction_details(page: Page, rows: Locator, width: int) -> None:
    await page.get_by_text("Credit cycle", exact=True).click()
    await page.get_by_text("Allowance bridge", exact=True).wait_for()
    if width >= 992:
        await rows.locator("tbody tr[data-transaction-row]").filter(has_text="UPS").click()
        await expect(rows.locator("tbody tr")).to_have_count(11)
    else:
        await _expand_accordion(rows.get_by_role("button", name="UPS", exact=False))
    await rows.get_by_text("Required document shipping for a synthetic example.").wait_for()
    await expect(rows.get_by_text("UPS · Example Shipping · Example Marketplace", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Counterparties:", exact=False)).to_have_count(0)
    await expect(rows.get_by_text("Example Shipping · merchant", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Mandatory · outside allowance", exact=False)).to_have_count(1)
    await expect(rows.get_by_text("Card statement: Counted in card cycle", exact=False).first).to_be_attached()
    await _expand_accordion(rows.get_by_role("button", name="Plaid source fields"))
    await rows.get_by_text("Plaid amount (major units): 18.50", exact=True).wait_for()
    await expect(rows.get_by_text("Original description: EXAMPLE SHIPPING PAYMENT", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("City: Example City", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Reference number: synthetic-reference", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Personal detail: TRANSPORTATION_SHIPPING", exact=True)).to_have_count(1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), _TRANSACTION_VIEWPORTS)
async def test_transaction_explanations_render(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Transactions")
    rows = page.get_by_role("table") if width >= 992 else page.locator(".mantine-Accordion-root").first
    await rows.get_by_text("Example Cafe", exact=True).wait_for()
    await expect(page.get_by_role("heading", name="Transactions", level=1)).to_have_count(1)
    await expect(rows.get_by_text("Refund held", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Document shipping", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Holiday travel", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("Full reversal", exact=True)).to_have_count(2)
    await expect(rows.locator('[data-category-id="travel"]')).to_have_count(1)
    await expect(page.get_by_text("1 · $15", exact=True)).to_have_count(1)
    transfer_flow = page.get_by_role("button", name="Filter transactions to Own-account transfers, USD")
    await expect(transfer_flow.get_by_text("$125.00", exact=True)).to_have_count(2)
    await expect(transfer_flow.get_by_text("2", exact=True)).to_have_count(1)
    tax_flow = page.get_by_role("button", name="Filter transactions to Taxes, USD")
    await expect(tax_flow.get_by_text("$415.00", exact=True)).to_have_count(1)
    await expect(tax_flow.get_by_text("$0.00", exact=True)).to_have_count(1)
    await expect(tax_flow.get_by_text("1 pending", exact=True)).to_have_count(1)
    cad_tax_flow = page.get_by_role("button", name="Filter transactions to Taxes, CAD")
    await expect(cad_tax_flow).to_contain_text("72.00")
    await expect(page.get_by_text("can still be real cash flows", exact=False)).to_be_visible()
    if width >= 992:
        await expect(rows.locator("tbody tr")).to_have_count(10)
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    image = f"transactions-{width}.png"
    await view.capture(image.removesuffix(".png"), label=f"{width}px all rows", full_page=True, animations="disabled")
    await _open_transaction_details(page, rows, width)
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expanded = f"transactions-expanded-{width}.png"
    await view.capture(
        expanded.removesuffix(".png"), label=f"{width}px rule explanation", full_page=True, animations="disabled"
    )
    await page.get_by_label("Transaction filter").get_by_text("Review", exact=True).click()
    await page.get_by_text("Showing 2 of 10", exact=True).wait_for()
    await expect(rows.get_by_text("UPS", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE OWN ACCOUNT TRANSFER", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("Confirm the purchase before netting this refund.")).not_to_be_visible()
    view.errors.assert_none(context="Spend")
    review = f"transactions-review-{width}.png"
    await view.capture(
        review.removesuffix(".png"), label=f"{width}px review filter", full_page=True, animations="disabled"
    )

    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()
    await transfer_flow.click()
    await page.get_by_text("Showing 2 of 10", exact=True).wait_for()
    await expect(rows.get_by_text("EXAMPLE OWN TRANSFER DEBIT", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("EXAMPLE OWN TRANSFER CREDIT", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("EXAMPLE OWN ACCOUNT TRANSFER", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE OWN ACCOUNT REVERSAL", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(0)
    await expect(page.get_by_text("Clear exclusion filter", exact=True)).to_be_visible()
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"transactions-excluded-flow-selected-{width}",
        label=f"{width}px excluded flow drilldown",
        full_page=True,
        animations="disabled",
    )

    await page.get_by_label("Transaction filter").get_by_text("Allowance effect", exact=True).click()
    await page.get_by_text("Showing 0 of 10", exact=True).wait_for()
    await page.get_by_text("Clear exclusion filter", exact=True).click()
    await page.get_by_text("Showing 2 of 10", exact=True).wait_for()
    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()

    await tax_flow.click()
    await page.get_by_text("Showing 1 of 10", exact=True).wait_for()
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT CAD", exact=True)).to_have_count(0)
    await page.get_by_label("Transaction filter").get_by_text("Review", exact=True).click()
    await page.get_by_text("Showing 0 of 10", exact=True).wait_for()
    await page.get_by_text("Clear exclusion filter", exact=True).click()
    await page.get_by_text("Showing 2 of 10", exact=True).wait_for()
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(0)

    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()
    await page.get_by_role("button", name="Filter transactions to Taxes, USD").click()
    await page.get_by_text("Clear exclusion filter", exact=True).wait_for()
    await page.get_by_text("7 days", exact=True).click()
    await page.get_by_text("Showing 9 of 9", exact=True).wait_for()
    await expect(page.get_by_text("Clear exclusion filter", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(1)
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.asyncio
async def test_empty_policy_excluded_flows_render(page: Page, view: VisualPage, dashboard_url: str) -> None:
    await page.set_viewport_size({"width": 390, "height": 844})

    async def remove_excluded_flows(route: Route) -> None:
        response = await route.fetch()
        payload = await response.json()
        payload["summary"]["excluded_flows"] = []
        await route.fulfill(response=response, json=payload)

    await page.route("**/api/v1/transactions*", remove_excluded_flows)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Transactions")
    await page.get_by_text("No policy-excluded flows in this period.", exact=True).wait_for()
    await expect(page.get_by_role("button", name="Filter transactions to Taxes, USD")).to_have_count(0)
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        "transactions-excluded-flows-empty-mobile",
        label="390px empty policy-excluded flows panel",
        full_page=True,
        animations="disabled",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_full_reversal_transaction_explanation(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Transactions")
    rows = page.get_by_role("table") if width >= 992 else page.locator(".mantine-Accordion-root").first

    async def open_transaction(name: str) -> Locator:
        if width >= 992:
            control = page.locator("tr[data-transaction-row]").filter(has_text=name)
            await control.click()
            return rows.locator("tbody").filter(has=control)
        control = page.get_by_role("button", name=name, exact=False)
        await _expand_accordion(control)
        return rows.locator(".mantine-Accordion-item").filter(has=control)

    original_details = await open_transaction("EXAMPLE OWN ACCOUNT TRANSFER")
    original = original_details.get_by_label("Full reversal pair example-full-reversal")
    await expect(original).to_be_visible()
    await expect(original.get_by_text("Original debit", exact=True)).to_be_visible()
    await expect(original.get_by_text("Original debit selector:", exact=False)).to_be_visible()
    await expect(original.get_by_text("Reversal credit selector:", exact=False)).to_be_visible()
    await expect(original_details.get_by_text("Classification rule is shown as context", exact=False)).to_be_visible()
    await expect(original_details.get_by_text("Allowance balance effect: $0", exact=False)).to_be_visible()
    await view.capture(
        f"full-reversal-transaction-explanation-{width}",
        label=f"{width}px full reversal explanation",
        full_page=True,
        animations="disabled",
    )

    reversal_details = await open_transaction("EXAMPLE OWN ACCOUNT REVERSAL")
    reversal = reversal_details.get_by_label("Full reversal pair example-full-reversal")
    await expect(reversal).to_be_visible()
    await expect(reversal.get_by_text("Reversal credit", exact=True)).to_be_visible()
    await expect(
        reversal_details.get_by_text("Synthetic rule classification retained as context for the reversal.")
    ).to_be_visible()
    await expect(reversal_details.get_by_text("Allowance balance effect: $0", exact=False)).to_be_visible()
    await view.capture(
        f"full-reversal-credit-explanation-{width}",
        label=f"{width}px full reversal credit explanation",
        full_page=True,
        animations="disabled",
    )


@pytest.mark.asyncio
async def test_transaction_explanations_dark_theme(page: Page, view: VisualPage, dashboard_url: str) -> None:
    await page.emulate_media(color_scheme="dark")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Transactions")
    rows = page.locator(".mantine-Accordion-root")
    await rows.get_by_text("Example Cafe", exact=True).wait_for()
    await _expand_accordion(rows.get_by_role("button", name="UPS", exact=False))
    await page.get_by_text("Required document shipping for a synthetic example.").wait_for()
    view.errors.assert_none(context="Spend")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    image = "transactions-dark-390.png"
    await view.capture(image.removesuffix(".png"), label="390px dark theme", full_page=True, animations="disabled")


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_experimental_render(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    requests: list[str] = []
    page.on(
        "request", lambda request: requests.append(request.url) if "/api/v1/sustainability" in request.url else None
    )
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await page.get_by_text("Tax-adjusted implied draw rate", exact=True).wait_for()
    await page.locator('canvas[aria-label="Annual funding including taxes by day"]').wait_for()
    await expect(page.get_by_text("Experimental", exact=True)).to_be_visible()
    await expect(page.get_by_text("TODAY'S portfolio valuation", exact=False)).to_be_visible()
    await expect(page.get_by_text("Scenario 4: 4.5%", exact=True)).to_be_visible()
    await expect(page.get_by_role("region", name="Account history coverage")).to_have_count(0)
    await expect(page.get_by_text("$2,750", exact=True)).to_be_visible()
    scenario_marker = page.get_by_role("button", name="Scenario 1: 3% draw rate")
    await expect(scenario_marker).to_be_visible()
    await expect(scenario_marker).to_have_text("3%")
    await expect(page.get_by_text("MANDATORY", exact=True)).to_have_count(1)
    await expect(page.get_by_text("FLEXIBLE", exact=True)).to_have_count(1)
    await expect(page.get_by_text("PROVISIONAL", exact=True)).to_have_count(1)
    await expect(page.get_by_text("No other income is modeled.", exact=False)).to_be_visible()
    source_details = page.locator("details").filter(has_text="Synthetic investment source")
    await expect(page.get_by_text("Synthetic investment source", exact=True)).to_be_hidden()
    await source_details.locator("summary").click()
    await expect(page.get_by_text("Synthetic investment source", exact=True)).to_be_visible()
    await source_details.locator("summary").click()
    assert any(url.endswith("/api/v1/sustainability/windows") for url in requests)
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-ready-{width}",
        label=f"{width}px synthetic ready estimate",
        full_page=True,
        animations="disabled",
    )

    for topic, explanation in [
        ("portfolio value", "Current net value is the denominator"),
        ("spending annualization", "Annualized from 90 covered complete days"),
        ("scenario 1 comparison", "Current implied draw is 1.5 percentage points above this scenario."),
        ("draw gauge scale", "Reference marks are comparisons, not safety thresholds or guarantees."),
    ]:
        help_button = page.get_by_role("button", name=f"About {topic}", exact=True)
        await help_button.focus()
        tooltip = page.get_by_role("tooltip").filter(has_text=explanation)
        await _expect_tooltip_rendered(tooltip)
        await view.capture(
            f"sustainability-help-{topic.replace(' ', '-')}-{width}",
            label=f"{width}px {topic} help",
            full_page=True,
            animations="disabled",
        )
        await page.keyboard.press("Escape")
        await expect(tooltip).to_be_hidden()

    history = page.get_by_role("region", name="Spending and funding history")
    await expect(history.get_by_label("Show 90-day average")).to_be_checked()
    await expect(history.get_by_text("Spending-only draw rate", exact=True)).to_have_count(0)
    include_taxes = history.get_by_label("Include withdrawal taxes in dollar amounts")
    await expect(include_taxes).to_be_checked()
    await include_taxes.uncheck()
    await expect(include_taxes).not_to_be_checked()
    await expect(history.get_by_text("Annual spending excluding taxes", exact=True)).to_be_visible()
    await expect(history.get_by_role("img", name="Annual spending excluding taxes by day")).to_be_visible()
    await view.capture(
        f"sustainability-spending-excluding-taxes-{width}",
        label=f"{width}px spending dollars excluding taxes with tax-inclusive withdrawal percentages",
        full_page=True,
        animations="disabled",
    )
    await include_taxes.check()
    await expect(history.get_by_role("img", name="Annual funding including taxes by day")).to_be_visible()

    await scenario_marker.hover()
    scenario_tooltip = page.get_by_role("tooltip").filter(has_text="Scenario 1: 3% draw rate")
    await _expect_tooltip_rendered(scenario_tooltip)
    await expect(scenario_tooltip).to_contain_text("Scenario 1: 3% draw rate")
    await expect(scenario_tooltip).to_contain_text("Supported monthly spending (USD/month):")
    await expect(scenario_tooltip).to_contain_text("$2,750")
    await expect(scenario_tooltip).to_contain_text("1.5 percentage points above this scenario")
    await expect(scenario_tooltip).to_contain_text("After 12% modeled withdrawal tax.")
    if width == 1280:
        await view.capture(
            "sustainability-scenario-tooltip-desktop",
            label="1280px scenario details opened by hovering the 3% marker",
            full_page=True,
            animations="disabled",
        )
    await page.mouse.move(0, 0)
    await _expect_tooltip_hidden(scenario_tooltip)
    await scenario_marker.focus()
    await page.keyboard.press("Tab")
    await page.keyboard.press("Shift+Tab")
    await expect(scenario_marker).to_be_focused()
    await _expect_tooltip_rendered(scenario_tooltip)
    if width == 390:
        await view.capture(
            "sustainability-scenario-tooltip-mobile",
            label="390px scenario details opened from the 3% marker",
            full_page=True,
            animations="disabled",
        )
        await page.keyboard.press("Escape")
        await _expect_tooltip_hidden(scenario_tooltip)
    view.errors.assert_none(context="Sustainability")

    request_count = len(requests)
    await page.get_by_text("30 days", exact=True).click()
    await expect(page.get_by_text("30 of 30 days covered", exact=False)).to_be_visible()
    await expect(history.get_by_label("Show 90-day average")).to_be_checked()
    assert len(requests) == request_count
    view.errors.assert_none(context="Sustainability")


@pytest.mark.parametrize("width", [1280, 390])
async def test_sustainability_graph_visibility_is_independent_of_details(
    page: Page, view: VisualPage, dashboard_url: str, width: int
) -> None:
    await page.set_viewport_size({"width": width, "height": 960})
    await page.goto(f"{dashboard_url}/#/sustainability", wait_until="domcontentloaded")
    history = page.get_by_role("region", name="Spending and funding history")
    details = page.get_by_role("region", name="Spending details", exact=True)
    canvas = history.get_by_role("img")
    await expect(canvas).to_be_visible()
    await canvas.evaluate("canvas => { canvas.dataset.preserved = 'yes'; }")
    await history.get_by_label("Show 90-day average").uncheck()
    await expect(details.get_by_role("heading", name="90-day estimate", exact=True)).to_be_visible()
    await details.get_by_text("30 days", exact=True).click()
    await expect(details.get_by_role("heading", name="30-day estimate", exact=True)).to_be_visible()
    await expect(details.get_by_text("$2,000", exact=True)).to_be_visible()
    await expect(history.get_by_label("Show 90-day average")).not_to_be_checked()
    async with page.expect_response(lambda response: response.url.endswith("/api/v1/sustainability/windows")):
        await page.get_by_role("button", name="Refresh", exact=True).click()
    await expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    await expect(history.get_by_label("Show 90-day average")).not_to_be_checked()
    await expect(canvas).to_have_attribute("data-preserved", "yes")
    await expect(page.get_by_role("region", name="Portfolio used in the estimate")).to_have_count(1)
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-independent-window-controls-{width}",
        label=f"{width}px 30-day details with the 90-day graph curve hidden",
        full_page=True,
        animations="disabled",
    )
    await history.get_by_label("Show 30-day average").uncheck()
    await history.get_by_label("Show 365-day average").uncheck()
    await expect(history.get_by_role("img")).to_have_count(0)
    await expect(details.get_by_role("heading", name="30-day estimate", exact=True)).to_be_visible()
    await history.get_by_label("Show 90-day average").check()
    await expect(history.get_by_role("img")).to_be_visible()
    view.errors.assert_none(context="Independent sustainability controls")


@pytest.mark.parametrize("width", [1280, 390])
async def test_sustainability_shorter_curve_survives_missing_yearly_history(
    page: Page, view: VisualPage, dashboard_url: str, width: int
) -> None:
    async def serve_windows(route: Route) -> None:
        yearly = _example_sustainability()
        yearly.update(window_days=365, history=[])
        await route.fulfill(
            json=_sustainability_dashboard(_example_sustainability_window(30), _example_sustainability(), yearly)
        )

    await page.route("**/api/v1/sustainability/windows", serve_windows)
    await page.set_viewport_size({"width": width, "height": 960})
    await page.goto(f"{dashboard_url}/#/sustainability?window_days=365", wait_until="domcontentloaded")
    history = page.get_by_role("region", name="Spending and funding history")
    await expect(history.get_by_role("img")).to_be_visible()
    await expect(page.get_by_text("Partial window · 90/365 days", exact=True)).to_be_visible()
    await expect(history.get_by_text("History requires at least 365 consecutive days", exact=False)).to_be_visible()
    await view.capture(
        f"sustainability-missing-yearly-history-{width}",
        label=f"{width}px shorter moving averages remain available with missing yearly history",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Missing yearly sustainability history")


@pytest.mark.parametrize("width", [1280, 390])
async def test_sustainability_shared_history_tooltip_dates(
    page: Page, view: VisualPage, dashboard_url: str, width: int
) -> None:
    async def serve_single_endpoint(route: Route) -> None:
        windows = [_example_sustainability_window(days) for days in (30, 90, 365)]
        for window in windows:
            history = window["history"]
            assert isinstance(history, list)
            window["history"] = history[-1:]
        await route.fulfill(json=_sustainability_dashboard(*windows))

    await page.route("**/api/v1/sustainability/windows", serve_single_endpoint)
    await page.set_viewport_size({"width": width, "height": 960})
    await page.goto(f"{dashboard_url}/#/sustainability", wait_until="domcontentloaded")
    canvas = page.get_by_role("img", name="Annual funding including taxes by day")
    await expect(canvas).to_be_visible()
    bounds = await canvas.bounding_box()
    assert bounds is not None
    await canvas.hover(position={"x": bounds["width"] / 2, "y": bounds["height"] / 2})
    tooltip = page.get_by_role("tooltip", name="History ending October 14, 2026")
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_contain_text("30-day window · Sep 15, 2026 \N{EN DASH} Oct 14, 2026")
    await expect(tooltip).to_contain_text("90-day window · Jul 17, 2026 \N{EN DASH} Oct 14, 2026")
    await expect(tooltip).to_contain_text("365-day window · Oct 15, 2025 \N{EN DASH} Oct 14, 2026")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-shared-history-tooltip-{width}",
        label=f"{width}px common endpoint with each average's full inclusive dates",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Sustainability shared history tooltip")


@pytest.mark.parametrize("width", [1280, 390])
async def test_sustainability_window_survives_reload_and_history(page: Page, dashboard_url: str, width: int) -> None:
    requests: list[str] = []
    page.on(
        "request", lambda request: requests.append(request.url) if "/api/v1/sustainability" in request.url else None
    )
    await page.set_viewport_size({"width": width, "height": 960})
    await page.goto(f"{dashboard_url}/#/sustainability?window_days=30", wait_until="domcontentloaded")
    await expect(page.get_by_text("30 of 30 days covered", exact=False)).to_be_visible()
    assert requests
    assert all(url.endswith("/api/v1/sustainability/windows") for url in requests)
    await page.get_by_text("90 days", exact=True).click()
    await expect(page).to_have_url(f"{dashboard_url}/#/sustainability?window_days=90")
    await expect(page.get_by_text("90 of 90 days covered", exact=False)).to_be_visible()
    await page.reload(wait_until="domcontentloaded")
    await expect(page.get_by_text("90 of 90 days covered", exact=False)).to_be_visible()
    await page.go_back()
    await expect(page.get_by_text("30 of 30 days covered", exact=False)).to_be_visible()
    await expect(page).to_have_url(f"{dashboard_url}/#/sustainability?window_days=30")
    await page.go_forward()
    await expect(page.get_by_text("90 of 90 days covered", exact=False)).to_be_visible()
    await expect(page).to_have_url(f"{dashboard_url}/#/sustainability?window_days=90")


@pytest.mark.parametrize("value", ["", "garbage", "-30", "1.5", "7", "9007199254740992"])
async def test_sustainability_invalid_url_window_uses_default(page: Page, dashboard_url: str, value: str) -> None:
    await page.goto(f"{dashboard_url}/#/sustainability?window_days={value}", wait_until="domcontentloaded")
    await expect(page.get_by_text("90 of 90 days covered", exact=False)).to_be_visible()
    await expect(page).to_have_url(f"{dashboard_url}/#/sustainability?window_days=90")


@pytest.mark.parametrize("width", [1280, 390])
async def test_sustainability_loading_preserves_layout(
    page: Page, view: VisualPage, dashboard_url: str, width: int
) -> None:
    response_ready = asyncio.Event()
    request_started = asyncio.Event()

    async def delay_estimate(route: Route) -> None:
        request_started.set()
        await response_ready.wait()
        await route.continue_()

    await page.route("**/api/v1/sustainability/windows", delay_estimate)
    await page.set_viewport_size({"width": width, "height": 960})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await request_started.wait()
    skeleton = page.get_by_role("status", name="Loading sustainability estimate")
    await expect(skeleton).to_be_visible()
    await expect(page.get_by_text("Loading estimate…", exact=True)).to_have_count(0)
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-initial-loading-{width}",
        label=f"{width}px initial loading skeleton",
        full_page=True,
        animations="disabled",
    )
    response_ready.set()
    await expect(skeleton).to_be_hidden()
    await expect(page.get_by_text("90 of 90 days covered", exact=False)).to_be_visible()
    request_started.clear()
    await page.get_by_text("30 days", exact=True).click()
    await expect(page.get_by_text("30 of 30 days covered", exact=False)).to_be_visible()
    await expect(skeleton).to_be_hidden()
    assert not request_started.is_set()

    response_ready.clear()
    request_started.clear()
    await page.get_by_role("button", name="Refresh", exact=True).click()
    await request_started.wait()
    await expect(skeleton).to_be_hidden()
    await expect(page.get_by_text("30 of 30 days covered", exact=False)).to_be_visible()
    await expect(page.get_by_role("img", name="Annual funding including taxes by day")).to_be_visible()
    response_ready.set()
    await expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    view.errors.assert_none(context="Sustainability loading")


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_partial_window_has_no_startup_chart(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    async def serve_partial(route: Route) -> None:
        result = _example_sustainability()
        result["covered_days"] = 45
        result["period_start"] = "2026-08-31"
        result.update(
            mandatory_minor_units=380_000,
            flexible_minor_units=205_000,
            provisional_minor_units=25_000,
            spending_minor_units=610_000,
        )
        result["history"] = []
        await route.fulfill(json=_sustainability_dashboard(result))

    await page.route("**/api/v1/sustainability/windows", serve_partial)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await expect(page.get_by_text("Partial window · 45/90 days", exact=True)).to_be_visible()
    await expect(page.get_by_text("History requires at least 90 consecutive days", exact=False)).to_be_visible()
    await expect(page.locator('canvas[aria-label="Annual funding including taxes by day"]')).to_have_count(0)
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-partial-window-{width}",
        label=f"{width}px partial headline with no incomplete rolling-window chart",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Sustainability partial window")


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_account_history_coverage_render(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    async def serve_partial_coverage(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability_with_partial_account_history()))

    await page.route("**/api/v1/sustainability/windows", serve_partial_coverage)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await expect(page.get_by_text("Partial window · 44/90 days", exact=True)).to_be_visible()
    await expect(page.locator('canvas[aria-label="Annual funding including taxes by day"]')).to_have_count(0)

    coverage = page.get_by_role("region", name="Account history coverage")
    await expect(coverage).to_be_visible()
    await expect(coverage.get_by_role("alert")).to_contain_text("Missing history stays unknown")
    await expect(coverage.get_by_role("alert")).to_contain_text("Example checking (43 days unavailable)")
    await expect(coverage.get_by_role("alert")).to_contain_text("Example card (29 days unavailable)")
    await expect(coverage.get_by_text("Coverage evidence for 2 accounts", exact=True)).to_be_visible()

    await expect(coverage.get_by_text("Approximate opening range: 2026-07-20 to 2026-07-27", exact=True)).to_be_hidden()
    await coverage.locator("details").first.locator(":scope > summary").click()
    account_rows = coverage.locator("details details")
    checking = account_rows.filter(has_text="Example checking")
    await checking.locator("summary").click()
    await expect(
        checking.get_by_text("Approximate opening range: 2026-07-20 to 2026-07-27", exact=True)
    ).to_be_visible()
    await expect(
        checking.get_by_text("Evidence: Synthetic account archive · as of 2026-10-14.", exact=False)
    ).to_be_visible()
    await expect(
        checking.get_by_text("Inferred history boundary from first recorded transaction: 2026-09-01", exact=True)
    ).to_be_visible()

    card = account_rows.filter(has_text="Example card")
    await card.locator("summary").click()
    await expect(card.get_by_text("Documented complete history from 2026-08-15", exact=True)).to_be_visible()
    await expect(
        card.get_by_text("Evidence: Synthetic statement archive · as of 2026-10-14.", exact=False)
    ).to_be_visible()
    await expect(card.get_by_text("First recorded transaction: 2026-08-15.", exact=True)).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    view.errors.assert_none(context="Sustainability account history coverage")
    await view.capture(
        f"sustainability-account-history-coverage-{width}",
        label=f"{width}px partial requested window with inferred and documented account history",
        full_page=True,
        animations="disabled",
    )


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_history_survives_current_coverage_gap(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    async def serve_historical_gap(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability_with_historical_coverage_gap()))

    await page.route("**/api/v1/sustainability/windows", serve_historical_gap)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await expect(page.get_by_role("alert").get_by_text("Estimate unavailable", exact=False)).to_be_visible()
    await expect(page.get_by_text("0 of 90 days covered · 2026-07-17 through 2026-10-14", exact=False)).to_be_visible()
    await expect(page.get_by_text("Valuation available", exact=True)).to_be_visible()
    await expect(page.get_by_text("Withdrawal taxes: 12% modeled", exact=True)).to_be_visible()
    coverage = page.get_by_role("region", name="Account history coverage")
    await expect(coverage.get_by_role("alert")).to_contain_text("Example new account (5 days unavailable)")

    history = page.get_by_role("region", name="Spending and funding history")
    await expect(history.get_by_label("Show 90-day average")).to_be_checked()
    await expect(history.get_by_role("img", name="Annual funding including taxes by day")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    view.errors.assert_none(context="Sustainability current coverage gap with historical values")
    await view.capture(
        f"sustainability-history-current-coverage-gap-{width}",
        label=f"{width}px older complete history with a current unavailable coverage gap",
        full_page=True,
        animations="disabled",
    )


async def test_sustainability_scenario_marker_supports_touch(touch_page: Page, dashboard_url: str) -> None:
    async def serve_ready(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability()))

    await touch_page.route("**/api/v1/sustainability/windows", serve_ready)
    await touch_page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(touch_page, "Sustainability")
    marker = touch_page.get_by_role("button", name="Scenario 1: 3% draw rate")
    await expect(marker).to_be_visible()
    await marker.tap()
    tooltip = touch_page.get_by_role("tooltip")
    await _expect_tooltip_rendered(tooltip)
    await expect(tooltip).to_contain_text("Scenario 1: 3% draw rate")
    await expect(tooltip).to_contain_text("Supported monthly spending (USD/month):")
    await expect(tooltip).to_contain_text("After 12% modeled withdrawal tax.")
    await VisualPage(touch_page, output_dir=undeclared_outputs_dir(), title="Spend").capture(
        "sustainability-scenario-touch-tooltip",
        label="Scenario details opened by tapping the 3% marker",
        full_page=True,
        animations="disabled",
    )
    await touch_page.get_by_role("heading", name="Sustainability", exact=True).tap()
    await _expect_tooltip_hidden(tooltip)
    await marker.tap()
    await _expect_tooltip_rendered(tooltip)
    await marker.tap()
    await _expect_tooltip_hidden(tooltip)
    await marker.tap()
    await _expect_tooltip_rendered(tooltip)
    await touch_page.keyboard.press("Escape")
    await _expect_tooltip_hidden(tooltip)


async def test_sustainability_short_gauge_fill_render(page: Page, view: VisualPage, dashboard_url: str) -> None:
    async def serve_low_draw(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability("low_draw")))

    await page.route("**/api/v1/sustainability/windows", serve_low_draw)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    gauge = page.get_by_role("region", name="Tax-adjusted implied draw rate")
    await expect(gauge.get_by_text("0.5%", exact=True)).to_be_visible()
    await expect(page.get_by_role("button", name="Scenario 1: 3% draw rate")).to_be_visible()
    await view.capture(
        "sustainability-short-gauge-fill",
        label="Synthetic 0.5% implied rate with a short gauge fill",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Sustainability short fill")


async def test_sustainability_scenario_tooltip_explains_unmodeled_tax(
    page: Page, view: VisualPage, dashboard_url: str
) -> None:
    async def serve_tax_unmodeled(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability("tax_unmodeled")))

    await page.route("**/api/v1/sustainability/windows", serve_tax_unmodeled)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    gauge = page.get_by_role("region", name="Tax-adjusted implied draw rate")
    await expect(gauge.get_by_text("Unavailable", exact=True).first).to_be_visible()
    await expect(page.get_by_text("Spending-only implied draw rate", exact=False)).to_have_count(0)
    marker = page.get_by_role("button", name="Scenario 1: 3% draw rate")
    await marker.hover()
    tooltip = page.get_by_role("tooltip")
    await expect(tooltip).to_contain_text("Supported monthly spending (USD/month):")
    await expect(tooltip).to_contain_text("Unavailable")
    await expect(tooltip).to_contain_text("Current draw rate is unavailable.")
    await expect(tooltip).to_contain_text("Spending comparison unavailable; withdrawal taxes are not modeled.")
    await page.mouse.move(0, 0)
    history = page.get_by_role("region", name="Spending and funding history")
    await expect(history.get_by_role("img")).to_have_count(0)
    await history.get_by_label("Include withdrawal taxes in dollar amounts").uncheck()
    await expect(history.get_by_role("img", name="Annual spending excluding taxes by day")).to_be_visible()
    await expect(history.get_by_text("Reference withdrawal scenarios", exact=True)).to_have_count(0)
    await view.capture(
        "sustainability-spending-without-tax-model",
        label="Consumption dollars available while tax-inclusive funding rates remain unavailable",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Sustainability with unmodeled tax")


@pytest.mark.parametrize("variant", ["stale", "unavailable"])
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_stale_and_unavailable_render(
    page: Page, view: VisualPage, dashboard_url: str, variant: str, width: int, height: int
) -> None:
    async def serve_variant(route: Route) -> None:
        await route.fulfill(json=_sustainability_dashboard(_example_sustainability(variant)))

    await page.route("**/api/v1/sustainability/windows", serve_variant)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await page.get_by_role("alert").get_by_text("Estimate unavailable", exact=False).wait_for()
    await expect(page.get_by_text("Portfolio used in the estimate", exact=True)).to_be_visible()
    await expect(page.get_by_text("Valuation unavailable", exact=True)).to_be_visible()
    await expect(page.get_by_text("ANNUALIZED SPENDING", exact=True)).to_be_visible()
    await expect(page.get_by_text("$49,511", exact=True)).to_be_visible()
    await expect(page.locator('[role="img"][aria-label*="gauge"]')).to_have_count(0)
    if variant == "stale":
        await page.get_by_text("Synthetic valuation has not refreshed recently.", exact=True).wait_for()
        await expect(page.get_by_text("46 days old", exact=False)).to_be_visible()
        await expect(page.get_by_text("90-day estimate issues", exact=True)).to_be_visible()
    else:
        await expect(page.get_by_text("Synthetic investment source", exact=True)).to_be_visible()
        await expect(page.get_by_text("Synthetic cash source", exact=True)).to_be_visible()
    view.errors.assert_none(context="Sustainability")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-{variant}-{width}",
        label=f"{width}px synthetic {variant} data",
        full_page=True,
        animations="disabled",
    )


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_contextual_help_keyboard_and_hover(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    help_button = page.get_by_role("button", name="About Estimate window", exact=True)
    await expect(help_button).to_be_visible()
    tooltip = page.get_by_role("tooltip")
    await expect(tooltip).to_be_hidden()
    await help_button.focus()
    await _expect_tooltip_rendered(tooltip)
    await expect(tooltip).to_contain_text("recorded spending pace and balance forecast")
    await view.capture(f"spending-help-{width}", label=f"{width}px keyboard help", animations="disabled")
    await page.keyboard.press("Escape")
    await _expect_tooltip_hidden(tooltip)
    await page.keyboard.press("Tab")
    await help_button.hover()
    await _expect_tooltip_rendered(tooltip)
    await page.mouse.move(0, 0)
    await _expect_tooltip_hidden(tooltip)
    view.errors.assert_none(context="Spend help")


async def test_contextual_help_touch(touch_page: Page, dashboard_url: str) -> None:
    await touch_page.goto(dashboard_url, wait_until="domcontentloaded")
    help_button = touch_page.get_by_role("button", name="About Estimate window", exact=True)
    tooltip = touch_page.get_by_role("tooltip")
    await help_button.tap()
    await _expect_tooltip_rendered(tooltip)
    await help_button.tap()
    await _expect_tooltip_hidden(tooltip)
    await help_button.tap()
    await _expect_tooltip_rendered(tooltip)
    await touch_page.get_by_role("heading", name="Flexible spending", exact=True).tap()
    await _expect_tooltip_hidden(tooltip)


async def test_accordion_readiness_ignores_unrelated_animations(page: Page) -> None:
    await page.set_content(
        get_required_path("_main/finance/plaid/spend/ui/accordion-readiness-fixture.html").read_text()
    )
    await _expand_accordion(page.get_by_role("button", name="Expand"))
    await expect(page.locator("#panel")).to_have_css("height", "120px")
    assert await page.locator("#spinner").evaluate("element => element.getAnimations().length") == 1


if __name__ == "__main__":
    pytest_bazel.main()
