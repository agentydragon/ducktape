"""Render the decision-first Spend page with synthetic allowance data.

Screenshot artifacts are published for PR review; this does not exercise private
login or assert that a deployed authenticated browser looks the same.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page, Route

from finance.plaid.spend.allowance import (
    AllOf,
    AllowanceView,
    AmountSign,
    AnyOf,
    Disposition,
    FieldExact,
    Kind,
    NamePrefix,
    Rule,
)
from finance.plaid.spend.models import (
    AllowanceConfigurationView,
    CardConfigurationView,
    SpendConfigurationView,
    SpendTransactionRow,
    SpendTransactionsView,
    StatementReason,
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

    @app.get("/api/v1/view")
    def view(warmup: bool = False) -> dict:
        payload: dict[str, Any] = {
            "generated_at": "2026-10-15T12:00:00Z",
            "allowance": {
                "status": "active",
                "note": None,
                "currency": "USD",
                "alert_state": "normal",
                "spending_signal": "normal",
                "monthly_minor_units": 70000,
                "activation_at": "2026-10-01",
                "current_cycle_start": "2026-10-01",
                "available_minor_units": 20000,
                "prior_carry_minor_units": 5000,
                "posted_minor_units": 52500,
                "pending_minor_units": 2500,
                "review_minor_units": 1500,
                "review_transaction_count": 2,
                "unmatched_refunds_minor_units": 0,
                "windows_minor_units": {
                    "current_credit_cycle_minor_units": 55000,
                    "trailing_7_days_minor_units": 8750,
                    "trailing_30_days_minor_units": 50000,
                    "calendar_month_minor_units": 55000,
                    "year_to_date_minor_units": 55000,
                },
                "trailing_7_daily_minor_units": 1250,
                "trailing_7_observed_daily_minor_units": 1250,
                "trailing_30_observed_daily_minor_units": 1000,
                "trailing_7_unmatched_count": 2,
                "trailing_7_unmatched_minor_units": 300,
                "projected_cycle_end_minor_units": 7500,
                "next_credit_at": "2026-10-25T00:00:00Z",
                "estimated_exhaustion_at": "2026-10-31T00:00:00Z",
                "last_synced_at": "2026-10-15T11:00:00Z",
            },
            "cards": [
                {
                    "label": "Example card",
                    "account_name": "Example card",
                    "mask": "0000",
                    "institution_name": "Sample Bank",
                    "currency": "USD",
                    "alert_state": "normal",
                    "alert_threshold_percent": None,
                    "spend_minor_units": 22000,
                    "limit_minor_units": 100000,
                    "spend_percent": 22,
                    "pending_minor_units": 1000,
                    "last_synced_at": "2026-10-15T11:00:00Z",
                    "cycle_start": "2026-10-01",
                    "statement_available": True,
                },
                {
                    "label": "New example card",
                    "account_name": "New example card",
                    "mask": "1111",
                    "institution_name": "Sample Credit Union",
                    "currency": "USD",
                    "alert_state": "unavailable",
                    "spend_minor_units": 3900,
                    "limit_minor_units": 100000,
                    "pending_minor_units": 0,
                    "last_synced_at": "2026-10-15T11:00:00Z",
                    "cycle_start": "2026-10-10",
                    "statement_available": False,
                },
            ],
        }
        if warmup:
            payload["allowance"].update(
                activation_at="2026-10-15",
                available_minor_units=70000,
                posted_minor_units=0,
                pending_minor_units=0,
                review_minor_units=0,
                review_transaction_count=0,
                prior_carry_minor_units=0,
                alert_state="unavailable",
                spending_signal="unavailable",
                trailing_7_daily_minor_units=None,
                trailing_7_observed_daily_minor_units=None,
                trailing_30_observed_daily_minor_units=None,
                trailing_7_unmatched_count=None,
                trailing_7_unmatched_minor_units=None,
                projected_cycle_end_minor_units=None,
                estimated_exhaustion_at=None,
            )
            payload["allowance"]["windows_minor_units"] = dict.fromkeys(payload["allowance"]["windows_minor_units"], 0)
        return payload

    @app.get("/api/v1/events")
    async def events() -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            yield f"event: view\ndata: {json.dumps(view())}\n\n"
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
    def transactions(window: Literal["7d", "30d", "cycle"] = "30d") -> SpendTransactionsView:
        return SpendTransactionsView(
            generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
            window=window,
            window_start={"7d": date(2026, 10, 9), "30d": date(2026, 9, 16), "cycle": date(2026, 10, 1)}[window],
            allowance=AllowanceView.model_validate(view()["allowance"]),
            rows=[
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
                    trailing_7_pace_minor_units=1500,
                    trailing_30_pace_minor_units=1500,
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
                    trailing_7_pace_minor_units=0,
                    trailing_30_pace_minor_units=0,
                    statement_minor_units=1850,
                    statement_reason=StatementReason.COUNTED,
                    pfc_primary="TRANSPORTATION",
                    pfc_detailed="TRANSPORTATION_SHIPPING",
                    merchant_category_code="4215",
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
                    trailing_7_pace_minor_units=0,
                    trailing_30_pace_minor_units=0,
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
                    trailing_7_pace_minor_units=0,
                    trailing_30_pace_minor_units=53500,
                    statement_minor_units=None,
                    statement_reason=None,
                    pfc_primary="TRAVEL",
                    pfc_detailed="TRAVEL_OTHER",
                    merchant_category_code=None,
                ),
            ],
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
    assert await page.get_by_text("$13 / day", exact=True).count() == 2
    assert await page.get_by_text("$10 / day", exact=True).count() == 1
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
    assert await page.get_by_text("Warming up", exact=True).count() == 3
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
    rows = page.get_by_role("table") if width >= 992 else page.locator(".mantine-Accordion-root")
    await rows.get_by_text("Example Cafe", exact=True).wait_for()
    assert await page.get_by_role("heading", name="Transactions", level=1).count() == 1
    assert await rows.get_by_text("Refund held", exact=True).count() == 1
    assert await page.get_by_text("2 · $15", exact=True).count() == 1
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
        await page.get_by_role("button", name="Show details for UPS").click()
        assert await rows.locator("tbody tr").count() == 5
    else:
        await rows.get_by_role("button", name="UPS", exact=False).click()
    await rows.get_by_text("Required document shipping for a synthetic example.").wait_for()
    assert await rows.get_by_text("Mandatory · outside allowance", exact=False).count() == 1
    assert await rows.get_by_text("Card statement: Counted in card cycle", exact=False).count() >= 1
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
