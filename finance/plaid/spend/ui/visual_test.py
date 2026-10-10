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
    OneOffOverride,
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
    AnalysisCategoryView,
    AppliedOverride,
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
from util.testing.frontend_visual import deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

# pytest_plugins loads util.playwright by name; Gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)

_UI_DIR = get_required_path("_main/finance/plaid/spend/ui/dist/index.html").parent


_FROZEN_NOW_MS = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp() * 1000)
_TRANSACTION_VIEWPORTS = [(1280, 960), (390, 844), (320, 720)]


@pytest.fixture
async def page(playwright: Playwright) -> AsyncIterator[Page]:
    async with deterministic_browser_context(
        playwright, viewport={"width": 1280, "height": 960}, frozen_now_ms=_FROZEN_NOW_MS
    ) as context:
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


async def _select_spend_page(page: Page, label: str) -> None:
    navigation_button = page.get_by_role("button", name="Open page navigation")
    if await navigation_button.is_visible():
        await navigation_button.click()
        await page.get_by_role("menuitem", name=label).click()
    else:
        await page.get_by_role("tab", name=label).click()


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
                overrides=[
                    OneOffOverride(
                        id="example-funding-transfer",
                        match=AllOf(
                            conditions=[
                                NameContains(field="name", substring="EXAMPLE FUNDING"),
                                DateRange(start=date(2026, 10, 3), end=date(2026, 10, 3)),
                            ]
                        ),
                        kind=Kind.EXCLUDED,
                        note="Own-account funding leg, confirmed by the owner 2026-10-04.",
                    )
                ],
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
                analysis_categories={
                    "unclassified": AnalysisCategory(label="Unclassified", color="#D97706"),
                    "refund_review": AnalysisCategory(label="Refund review", color="#B45309"),
                    "shipping": AnalysisCategory(label="Document shipping", color="#0F766E"),
                    "travel": AnalysisCategory(label="Holiday travel", color="#7C3AED"),
                },
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
                disposition=Disposition.EXCLUDED,
                rule_number=None,
                rule=None,
                override=AppliedOverride(
                    id="example-own-account-leg",
                    kind=Kind.EXCLUDED,
                    note="Second leg of one own-account transfer, confirmed by the owner 2026-10-12.",
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
                category=AnalysisCategoryView(id="unclassified", label="Unclassified", color="#D97706"),
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
                category=AnalysisCategoryView(id="travel", label="Holiday travel", color="#7C3AED"),
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
    await expect(page.get_by_text("Earlier purchases inform this graph", exact=False)).to_be_visible()
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
async def test_chart_transactions_start_before_view_finishes(page: Page, dashboard_url: str) -> None:
    release_view = asyncio.Event()

    async def delayed_view(route: Route) -> None:
        await release_view.wait()
        await route.continue_()

    await page.route("**/api/v1/view*", delayed_view)
    try:
        async with page.expect_request("**/api/v1/transactions?period=rolling_7d"):
            await page.goto(dashboard_url, wait_until="domcontentloaded")
    finally:
        release_view.set()
    await page.locator("canvas[aria-label]").wait_for()


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
    await page.add_init_script("window.EventSource = class { addEventListener() {} close() {} }")

    async def serve_warmup(route: Route) -> None:
        await route.continue_(url=f"{dashboard_url}/api/v1/view?warmup=true")

    await page.route("**/api/v1/view", serve_warmup)
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
    await page.get_by_text("Unverified credit; inspect the earlier purchase before netting it.").wait_for()
    await expect(page.get_by_text("Review", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Amount is negative AND (Transaction name starts with", exact=False)).to_have_count(1)
    await expect(page.get_by_text("One-off overrides", exact=True)).to_have_count(1)
    await page.get_by_text("example-funding-transfer", exact=True).wait_for()
    await expect(
        page.get_by_text("Own-account funding leg, confirmed by the owner 2026-10-04.", exact=True)
    ).to_have_count(1)
    await expect(page.get_by_text("Transaction name contains", exact=False)).to_have_count(1)
    await expect(page.get_by_text("Date is 2026-10-03", exact=False)).to_have_count(1)
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
        await expect(rows.locator("tbody tr")).to_have_count(6)
    else:
        await _expand_accordion(rows.get_by_role("button", name="UPS", exact=False))
    await rows.get_by_text("Required document shipping for a synthetic example.").wait_for()
    await expect(rows.get_by_text("Counterparties: Example Shipping", exact=True)).to_have_count(1)
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
    await expect(rows.locator('[data-category-id="travel"]')).to_have_count(1)
    await expect(rows.get_by_text("Excluded (override)", exact=True)).to_have_count(1)
    await expect(page.get_by_text("1 · $15", exact=True)).to_have_count(1)
    if width >= 992:
        await expect(rows.locator("tbody tr")).to_have_count(5)
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
    await page.get_by_text("Review", exact=True).click()
    await page.get_by_text("Showing 2 of 5", exact=True).wait_for()
    await expect(rows.get_by_text("UPS", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE OWN ACCOUNT TRANSFER", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("Confirm the purchase before netting this refund.")).not_to_be_visible()
    view.errors.assert_none(context="Spend")
    review = f"transactions-review-{width}.png"
    await view.capture(
        review.removesuffix(".png"), label=f"{width}px review filter", full_page=True, animations="disabled"
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


async def test_accordion_readiness_ignores_unrelated_animations(page: Page) -> None:
    await page.set_content("""
        <style>
          @keyframes pulse { to { opacity: 0.5 } }
          #spinner { animation: pulse 1s linear infinite }
          #panel { height: 0; overflow: hidden; transition: height 150ms }
        </style>
        <div id="spinner">An unrelated animation never finishes</div>
        <button aria-expanded="false" aria-controls="panel">Expand</button>
        <div id="panel"><div style="height: 120px">Panel contents</div></div>
        <script>
          document.querySelector('button').onclick = event => {
            event.currentTarget.setAttribute('aria-expanded', 'true');
            requestAnimationFrame(() => { document.querySelector('#panel').style.height = '120px'; });
          };
        </script>
    """)
    await _expand_accordion(page.get_by_role("button", name="Expand"))
    await expect(page.locator("#panel")).to_have_css("height", "120px")
    assert await page.locator("#spinner").evaluate("element => element.getAnimations().length") == 1


if __name__ == "__main__":
    pytest_bazel.main()
