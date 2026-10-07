"""Render the decision-first Spend page with synthetic allowance data.

Screenshot artifacts are published for PR review; this does not exercise private
login or assert that a deployed authenticated browser looks the same.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, Route

from finance.plaid.spend.allowance import (
    AllOf,
    AllowanceSpendPeriod,
    AllowanceView,
    AmountSign,
    AnyOf,
    Disposition,
    EstimatePeriodId,
    FieldExact,
    ForecastView,
    Kind,
    NamePrefix,
    PaceAlert,
    Period,
    PeriodId,
    PlaidCounterparty,
    RecordedPacePeriod,
    Rule,
    Status,
    TransactionPeriodId,
    UnmatchedCharges,
)
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    CardConfigurationView,
    CardView,
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
from util.testing.visual_review import retain_review_asset

# pytest_plugins loads util.playwright by name; Gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)

_UI_DIR = get_required_path("_main/finance/plaid/spend/ui/dist/index.html").parent


@pytest.fixture(scope="module")
def dashboard_url() -> Iterator[str]:
    app = FastAPI()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_UI_DIR / "index.html")

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
    async def events(estimate_period_id: EstimatePeriodId | None = None) -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            yield f"event: view\ndata: {json.dumps(view(estimate_period_id=estimate_period_id).model_dump(mode='json'))}\n\n"
            while True:
                await asyncio.sleep(60)
                yield ": keepalive\n\n"

        return StreamingResponse(updates(), media_type="text/event-stream")

    @app.get("/api/v1/configuration", response_model=SpendConfigurationView)
    def configuration() -> SpendConfigurationView:
        return SpendConfigurationView(
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
                    )
                ],
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
                analysis_category_label="Document shipping",
                counterparties=[
                    PlaidCounterparty(
                        name="Example Shipping",
                        type="merchant",
                        confidence_level="HIGH",
                        entity_id="synthetic-merchant",
                        website="https://example.invalid/shipping",
                    )
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
                analysis_category_label="Holiday travel",
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
            ),
            allowance=example_allowance(),
            rows=shown,
        )

    app.mount("/static", StaticFiles(directory=_UI_DIR))
    with serve_app_sync(app) as url:
        yield url


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_spending_decision_render(
    page: Page, dashboard_url: str, width: int, height: int, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    normal = tmp_path / f"dashboard-{width}.png"
    await page.screenshot(path=str(normal), full_page=True, animations="disabled")
    retain_review_asset(normal, title="Spend decisions", label=f"{width}px available", name=normal.name)
    await page.get_by_text("$200", exact=True).wait_for()
    assert await page.get_by_role("heading", name="Flexible spending", level=1).count() == 1
    assert await page.get_by_role("alert").get_by_text("2 charges ($15) need review").count() == 1
    assert await page.get_by_role("heading", name="Can I afford this?").count() == 1
    assert await page.get_by_text("7 days", exact=True).count() == 1
    assert await page.locator('span[title="$12.50"]').count() >= 1
    assert await page.locator('span[title="$200.00"]').count() >= 1
    assert await page.get_by_text("30 days", exact=True).count() == 1
    assert await page.get_by_text("$13 / day", exact=True).count() == 1
    assert await page.get_by_text("Below provisional leash", exact=True).count() == 1
    assert await page.get_by_text("7d unmatched 2 ($3)", exact=False).count() == 1
    assert await page.get_by_text("$75", exact=True).count() == 1
    assert await page.get_by_text("Provisional card total since", exact=False).count() == 1
    assert await page.get_by_text("Includes purchases outside the allowance", exact=False).count() == 1
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    assert await page.get_by_text("-$50", exact=True).count() == 1
    assert await page.get_by_text("$175 short before", exact=False).count() == 1
    assert not errors
    await page.get_by_label("Hypothetical flexible purchase").fill("200.01")
    assert await page.get_by_text("-<$1", exact=True).count() == 1
    assert await page.locator('span[title="-$0.01"]').count() == 1
    exceeded = tmp_path / f"dashboard-{width}-purchase.png"
    await page.screenshot(path=str(exceeded), full_page=True, animations="disabled")
    retain_review_asset(exceeded, title="Spend decisions", label=f"{width}px hypothetical purchase", name=exceeded.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_spending_decision_dark_theme(
    page: Page, dashboard_url: str, width: int, height: int, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.emulate_media(color_scheme="dark")
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_role("heading", name="Where you stand").wait_for()
    assert await page.locator("html").get_attribute("data-mantine-color-scheme") == "dark"
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    initial = tmp_path / f"dashboard-dark-{width}.png"
    await page.screenshot(path=str(initial), full_page=True, animations="disabled")
    retain_review_asset(initial, title="Spend decisions", label=f"{width}px dark theme", name=initial.name)

    await page.get_by_label("Hypothetical flexible purchase").fill("100")
    await page.get_by_text("Pace warning", exact=True).last.wait_for()
    assert not errors
    warning = tmp_path / f"dashboard-dark-{width}-warning.png"
    await page.screenshot(path=str(warning), full_page=True, animations="disabled")
    retain_review_asset(warning, title="Spend decisions", label=f"{width}px dark pace warning", name=warning.name)

    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    assert not errors
    exceeded = tmp_path / f"dashboard-dark-{width}-exceeded.png"
    await page.screenshot(path=str(exceeded), full_page=True, animations="disabled")
    retain_review_asset(exceeded, title="Spend decisions", label=f"{width}px dark over allowance", name=exceeded.name)


@pytest.mark.asyncio
async def test_new_allowance_has_no_fake_zero_pace(page: Page, dashboard_url: str, tmp_path: Path) -> None:
    await page.add_init_script("window.EventSource = class { addEventListener() {} close() {} }")

    async def serve_warmup(route: Route) -> None:
        await route.continue_(url=f"{dashboard_url}/api/v1/view?warmup=true")

    await page.route("**/api/v1/view", serve_warmup)
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_text("Not enough data", exact=True).wait_for()
    assert await page.get_by_text("Pace warming up", exact=True).count() == 1
    assert await page.get_by_text("Warming up", exact=True).count() == 1
    assert await page.get_by_text("$700", exact=True).count() >= 1
    await page.get_by_label("Hypothetical flexible purchase").fill("10")
    assert await page.get_by_text("$690", exact=True).count() == 1
    assert await page.get_by_text("Pace estimate warming up", exact=False).count() == 1
    image = tmp_path / "dashboard-warmup.png"
    await page.screenshot(path=str(image), full_page=True, animations="disabled")
    retain_review_asset(image, title="Spend decisions", label="New allowance warming up", name=image.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_review_rule_configuration_render(
    page: Page, dashboard_url: str, width: int, height: int, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_role("tab", name="Configuration").click()
    await page.get_by_text("Unverified credit; inspect the earlier purchase before netting it.").wait_for()
    assert await page.get_by_text("Review", exact=True).count() == 1
    assert await page.get_by_text("Amount is negative AND (Transaction name starts with", exact=False).count() == 1
    assert not errors
    image = tmp_path / f"configuration-review-{width}.png"
    await page.screenshot(path=str(image), full_page=True, animations="disabled")
    retain_review_asset(image, title="Spend configuration", label=f"{width}px review rule", name=image.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844), (320, 720)])
async def test_transaction_explanations_render(
    page: Page, dashboard_url: str, width: int, height: int, tmp_path: Path
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_role("tab", name="Transactions").click()
    rows = page.get_by_role("table") if width >= 992 else page.locator(".mantine-Accordion-root").first
    await rows.get_by_text("Example Cafe", exact=True).wait_for()
    assert await page.get_by_role("heading", name="Transactions", level=1).count() == 1
    assert await rows.get_by_text("Refund held", exact=True).count() == 1
    assert await rows.get_by_text("Document shipping", exact=True).count() == 1
    assert await rows.get_by_text("Holiday travel", exact=True).count() == 1
    assert await page.get_by_text("1 · $15", exact=True).count() == 1
    if width >= 992:
        assert await rows.locator("tbody tr").count() == 4
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    image = tmp_path / f"transactions-{width}.png"
    await page.screenshot(path=str(image), full_page=True, animations="disabled")
    retain_review_asset(image, title="Spend transactions", label=f"{width}px all rows", name=image.name)

    await page.get_by_text("Credit cycle", exact=True).click()
    await page.get_by_text("Allowance bridge", exact=True).wait_for()
    if width >= 992:
        await rows.locator("tbody tr[data-transaction-row]").filter(has_text="UPS").click()
        assert await rows.locator("tbody tr").count() == 5
    else:
        await rows.get_by_role("button", name="UPS", exact=False).click()
    await rows.get_by_text("Required document shipping for a synthetic example.").wait_for()
    assert await rows.get_by_text("Counterparties: Example Shipping", exact=True).count() == 1
    assert await rows.get_by_text("Example Shipping · merchant", exact=True).count() == 1
    assert await rows.get_by_text("Mandatory · outside allowance", exact=False).count() == 1
    assert await rows.get_by_text("Card statement: Counted in card cycle", exact=False).count() >= 1
    await rows.get_by_role("button", name="Plaid source fields").click()
    await rows.get_by_text("Plaid amount (major units): 18.50", exact=True).wait_for()
    assert await rows.get_by_text("Original description: EXAMPLE SHIPPING PAYMENT", exact=True).count() == 1
    assert await rows.get_by_text("City: Example City", exact=True).count() == 1
    assert await rows.get_by_text("Reference number: synthetic-reference", exact=True).count() == 1
    assert await rows.get_by_text("Personal detail: TRANSPORTATION_SHIPPING", exact=True).count() == 1
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expanded = tmp_path / f"transactions-expanded-{width}.png"
    await page.screenshot(path=str(expanded), full_page=True, animations="disabled")
    retain_review_asset(expanded, title="Spend transactions", label=f"{width}px rule explanation", name=expanded.name)

    await page.get_by_text("Review", exact=True).click()
    await page.get_by_text("Showing 2 of 4", exact=True).wait_for()
    assert await rows.get_by_text("UPS", exact=True).count() == 0
    assert not await rows.get_by_text("Confirm the purchase before netting this refund.").is_visible()
    assert not errors
    review = tmp_path / f"transactions-review-{width}.png"
    await page.screenshot(path=str(review), full_page=True, animations="disabled")
    retain_review_asset(review, title="Spend transactions", label=f"{width}px review filter", name=review.name)


@pytest.mark.asyncio
async def test_transaction_explanations_dark_theme(page: Page, dashboard_url: str, tmp_path: Path) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    await page.emulate_media(color_scheme="dark")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_role("tab", name="Transactions").click()
    rows = page.locator(".mantine-Accordion-root")
    await rows.get_by_text("Example Cafe", exact=True).wait_for()
    await rows.get_by_role("button", name="UPS", exact=False).click()
    await page.get_by_text("Required document shipping for a synthetic example.").wait_for()
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    image = tmp_path / "transactions-dark-390.png"
    await page.screenshot(path=str(image), full_page=True, animations="disabled")
    retain_review_asset(image, title="Spend transactions", label="390px dark theme", name=image.name)


if __name__ == "__main__":
    pytest_bazel.main()
