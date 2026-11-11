"""Render the decision-first Spend page with synthetic allowance data.

Screenshot artifacts are published for PR review; this does not exercise private
login or assert that a deployed authenticated browser looks the same.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Locator, Page, Playwright, Route, ViewportSize, expect
from python.runfiles import Runfiles

from devinfra.testing.output_writer import BoundTestOutputs
from finance.plaid.spend.allowance import EstimatePeriodId
from finance.plaid.spend.models import SpendConfigurationView, SpendTransactionsView, SpendView
from finance.plaid.spend.reconciliation import ReportStatus
from finance.plaid.spend.sustainability import SustainabilityDashboard, SustainabilityHistoryPoint
from finance.plaid.spend.testing import dashboard
from finance.plaid.spend.ui.testing import sustainability, transactions
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual.capture import VisualPage, open_visual_page
from util.testing.frontend_visual.context import deterministic_browser_context

# pytest_plugins loads util.playwright by name; Gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
pytest_plugins = ("util.playwright",)


_FROZEN_NOW_MS = int(datetime(2026, 10, 15, tzinfo=UTC).timestamp() * 1000)
_TRANSACTION_VIEWPORTS = [(1280, 960), (390, 844), (320, 720)]


@asynccontextmanager
async def _spend_view(
    playwright: Playwright, test_outputs: BoundTestOutputs, viewport: ViewportSize, *, has_touch: bool = False
) -> AsyncIterator[VisualPage]:
    async with (
        deterministic_browser_context(
            playwright, viewport=viewport, frozen_now_ms=_FROZEN_NOW_MS, has_touch=has_touch
        ) as context,
        open_visual_page(context, test_outputs, title="Spend", trace=True) as view,
    ):
        yield view


@pytest.fixture
async def view(playwright: Playwright, test_outputs: BoundTestOutputs) -> AsyncIterator[VisualPage]:
    async with _spend_view(playwright, test_outputs, {"width": 1280, "height": 960}) as view:
        yield view


@pytest.fixture
async def touch_view(playwright: Playwright, test_outputs: BoundTestOutputs) -> AsyncIterator[VisualPage]:
    async with _spend_view(playwright, test_outputs, {"width": 390, "height": 844}, has_touch=True) as view:
        yield view


@pytest.fixture
def page(view: VisualPage) -> Page:
    return view.page


@pytest.fixture
def touch_page(touch_view: VisualPage) -> Page:
    return touch_view.page


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
def dashboard_url(runfiles: Runfiles) -> Iterator[str]:
    ui_dir = Path(str(runfiles.root() / "ducktape/finance/plaid/spend/ui/dist/index.html")).parent
    app = FastAPI()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(ui_dir / "index.html")

    app.add_api_route("/api/v1/view", dashboard.spend_view, methods=["GET"], response_model=SpendView)
    app.add_api_route(
        "/api/v1/configuration", dashboard.configuration, methods=["GET"], response_model=SpendConfigurationView
    )
    app.add_api_route("/api/v1/transactions", transactions.view, methods=["GET"], response_model=SpendTransactionsView)

    @app.get("/api/v1/events")
    async def events(estimate_period_id: EstimatePeriodId | None = None, warmup: bool = False) -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            yield (
                f"event: view\ndata: "
                f"{json.dumps(dashboard.spend_view(warmup=warmup, estimate_period_id=estimate_period_id).model_dump(mode='json'))}\n\n"
            )
            while True:
                await asyncio.sleep(60)
                yield ": keepalive\n\n"

        return StreamingResponse(updates(), media_type="text/event-stream")

    @app.get("/api/v1/sustainability/windows")
    def sustainability_windows() -> SustainabilityDashboard:
        return sustainability.dashboard(*(sustainability.window(days) for days in (30, 90, 365)))

    app.mount("/static", StaticFiles(directory=ui_dir))
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
    await expect(page.get_by_role("alert").get_by_text("1 charge ($15) need review")).to_have_count(1)
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
    await expect(page.get_by_text("Provisional leash ~$23 / day.", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Pace ≤ provisional leash", exact=True)).to_have_count(1)
    await expect(page.get_by_text("Oldest linked sync", exact=False)).to_have_count(1)
    sync_help = page.get_by_role("button", name="About Linked sync time")
    await sync_help.click()
    await expect(page.get_by_text("The oldest successful sync of a Plaid Item", exact=False)).to_be_visible()
    await sync_help.click()
    await expect(page.get_by_text("Unmatched in selected window: 1 ($15)", exact=False)).to_have_count(1)
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
        "$88 flexible spending over 7 days; average $13 per day; leash reference $23 per day."
    )
    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    await expect(page.get_by_text("-$50", exact=True)).to_have_count(1)
    await expect(page.get_by_text("$175 short before", exact=False)).to_have_count(1)
    view.errors.assert_none(context="Spend")
    await page.get_by_label("Hypothetical flexible purchase").fill("200.01")
    await expect(page.get_by_text("-$0.01", exact=True)).to_have_count(1)
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
    await page.get_by_text("Forecast or pace warning", exact=True).last.wait_for()
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
async def test_purchase_check_distinguishes_exhausted_and_over_allowance(page: Page, dashboard_url: str) -> None:
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await page.get_by_text("$200", exact=True).wait_for()

    await page.get_by_label("Hypothetical flexible purchase").fill("200")
    await expect(page.get_by_text("Allowance exhausted", exact=True)).to_have_count(1)

    await page.get_by_label("Hypothetical flexible purchase").fill("200.01")
    await expect(page.get_by_text("Over allowance", exact=True)).to_have_count(1)


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
    await expect(page.get_by_text("Review", exact=True)).to_have_count(2)
    await expect(page.get_by_text('amount < 0 && (starts_with(name, "EXAMPLE") ||', exact=False)).to_have_count(1)
    await expect(page.get_by_text("One-off", exact=True)).to_have_count(2)
    one_off_id = page.get_by_text("example-funding-transfer", exact=True)
    await one_off_id.wait_for()
    # Long selectors need a readable column even when the badges wrap on mobile.
    one_off_box = await one_off_id.bounding_box()
    assert one_off_box is not None
    assert one_off_box["width"] >= 160
    await expect(
        page.get_by_text("Own-account funding leg, confirmed by the owner 2026-10-04.", exact=True)
    ).to_have_count(1)
    await expect(page.get_by_text('date == "2026-10-03"', exact=False)).to_have_count(1)
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
        original = rows.locator("tbody.transaction-rows").filter(
            has_text="UPS · Example Shipping · Example Marketplace"
        )
        await original.locator("tr[data-transaction-row]").click()
        await expect(rows.locator("tbody tr")).to_have_count(14)
    else:
        original = rows.locator(".mantine-Accordion-item").filter(
            has_text="UPS · Example Shipping · Example Marketplace"
        )
        await _expand_accordion(original.get_by_role("button", name="UPS", exact=False))
    await original.get_by_text("Required document shipping for a synthetic example.").wait_for()
    await expect(original.get_by_text("UPS · Example Shipping · Example Marketplace", exact=True)).to_have_count(1)
    await expect(original.get_by_text("Counterparties:", exact=False)).to_have_count(0)
    await expect(original.get_by_text("Example Shipping · merchant", exact=True)).to_have_count(1)
    await expect(original.get_by_text("Mandatory · outside allowance", exact=False)).to_have_count(1)
    await expect(original.get_by_text("Card statement: Counted in card cycle", exact=False)).to_be_attached()
    await _expand_accordion(original.get_by_role("button", name="Plaid source fields"))
    await original.get_by_text("Plaid amount (major units): 18.50", exact=True).wait_for()
    await expect(original.get_by_text("Original description: EXAMPLE SHIPPING PAYMENT", exact=True)).to_have_count(1)
    await expect(original.get_by_text("City: Example City", exact=True)).to_have_count(1)
    await expect(original.get_by_text("Reference number: synthetic-reference", exact=True)).to_have_count(1)
    await expect(original.get_by_text("Personal detail: TRANSPORTATION_SHIPPING", exact=True)).to_have_count(1)


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
    await expect(rows.get_by_text("Document shipping", exact=True)).to_have_count(2)
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
    await expect(rows.get_by_text("Other currency", exact=True)).to_have_count(1)
    await expect(page.get_by_text("can still be real cash flows", exact=False)).to_be_visible()
    if width >= 992:
        await expect(rows.locator("tbody tr")).to_have_count(14)
    await page.get_by_text("Showing 14 of 14", exact=True).wait_for()
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
    await page.get_by_text("Showing 2 of 13", exact=True).wait_for()
    await expect(rows.get_by_text("UPS", exact=True)).to_have_count(0)
    await expect(rows.get_by_text("EXAMPLE OWN ACCOUNT TRANSFER", exact=True)).to_have_count(0)
    await expect(
        rows.get_by_text("Unverified credit; inspect the earlier purchase before netting it.")
    ).not_to_be_visible()
    view.errors.assert_none(context="Spend")
    review = f"transactions-review-{width}.png"
    await view.capture(
        review.removesuffix(".png"), label=f"{width}px review filter", full_page=True, animations="disabled"
    )

    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()
    await transfer_flow.click()
    await page.get_by_text("Showing 2 of 13", exact=True).wait_for()
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
    await page.get_by_text("Showing 0 of 13", exact=True).wait_for()
    await page.get_by_text("Clear exclusion filter", exact=True).click()
    await page.get_by_text("Showing 3 of 13", exact=True).wait_for()
    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()

    await tax_flow.click()
    await page.get_by_text("Showing 1 of 13", exact=True).wait_for()
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(1)
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT CAD", exact=True)).to_have_count(0)
    await page.get_by_label("Transaction filter").get_by_text("Review", exact=True).click()
    await page.get_by_text("Showing 0 of 13", exact=True).wait_for()
    await page.get_by_text("Clear exclusion filter", exact=True).click()
    await page.get_by_text("Showing 2 of 13", exact=True).wait_for()
    await expect(rows.get_by_text("EXAMPLE TAX PAYMENT", exact=True)).to_have_count(0)

    await page.get_by_label("Transaction filter").get_by_text("All", exact=True).click()
    await page.get_by_role("button", name="Filter transactions to Taxes, USD").click()
    await page.get_by_text("Clear exclusion filter", exact=True).wait_for()
    await page.get_by_text("7 days", exact=True).click()
    await page.get_by_text("Showing 12 of 12", exact=True).wait_for()
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
    original = rows.locator(".mantine-Accordion-item").filter(has_text="UPS · Example Shipping · Example Marketplace")
    await _expand_accordion(original.get_by_role("button", name="UPS", exact=False))
    await original.get_by_text("Required document shipping for a synthetic example.").wait_for()
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
        yearly = sustainability.with_history(sustainability.view(window_days=365), [])
        await route.fulfill(
            json=sustainability.dashboard(sustainability.window(30), sustainability.view(), yearly).model_dump(
                mode="json"
            )
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
        windows = [
            sustainability.with_history(window, window.history[-1:])
            for window in (sustainability.window(days) for days in (30, 90, 365))
        ]
        await route.fulfill(json=sustainability.dashboard(*windows).model_dump(mode="json"))

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
        await route.fulfill(json=sustainability.dashboard(sustainability.partial_window()).model_dump(mode="json"))

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
        await route.fulfill(
            json=sustainability.dashboard(sustainability.with_partial_account_history()).model_dump(mode="json")
        )

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
        await route.fulfill(
            json=sustainability.dashboard(sustainability.with_historical_coverage_gap()).model_dump(mode="json")
        )

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


async def test_sustainability_scenario_marker_supports_touch(touch_view: VisualPage, dashboard_url: str) -> None:
    touch_page = touch_view.page

    async def serve_ready(route: Route) -> None:
        await route.fulfill(json=sustainability.dashboard(sustainability.view()).model_dump(mode="json"))

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
    await touch_view.capture(
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
        await route.fulfill(json=sustainability.dashboard(sustainability.view("low_draw")).model_dump(mode="json"))

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
        await route.fulfill(json=sustainability.dashboard(sustainability.view("tax_unmodeled")).model_dump(mode="json"))

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
    await expect(history.get_by_label("Include withdrawal taxes in dollar amounts")).to_be_disabled()
    await expect(history.get_by_label("Include withdrawal taxes in dollar amounts")).not_to_be_checked()
    await expect(history.get_by_role("img", name="Annual spending excluding taxes by day")).to_be_visible()
    await expect(history.get_by_text("Reference withdrawal scenarios", exact=True)).to_have_count(0)
    await view.capture(
        "sustainability-spending-without-tax-model",
        label="Consumption dollars available while tax-inclusive funding rates remain unavailable",
        full_page=True,
        animations="disabled",
    )
    view.errors.assert_none(context="Sustainability with unmodeled tax")


@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_provisional_reversal_keeps_estimate_and_marks_history(
    page: Page, view: VisualPage, dashboard_url: str, width: int, height: int
) -> None:
    async def serve_provisional(route: Route) -> None:
        provisional = sustainability.view("provisional")
        provisional = sustainability.with_history(provisional, provisional.history[-1:])
        await route.fulfill(json=sustainability.dashboard(provisional).model_dump(mode="json"))

    await page.route("**/api/v1/sustainability/windows", serve_provisional)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")

    await expect(page.get_by_text("Provisional", exact=True).first).to_be_visible()
    warning = page.get_by_role("alert").filter(has_text="Ordinary accounting remains in this estimate")
    await expect(warning).to_contain_text("while a reversal is unverified")
    reversal_issue = page.get_by_role("alert").filter(has_text="Review the unverified reversal")
    await expect(reversal_issue).to_be_visible()

    gauge = page.get_by_role("region", name="Tax-adjusted implied draw rate")
    await expect(gauge).to_be_visible()
    await expect(gauge).to_contain_text("4.5%")
    await expect(gauge).to_contain_text("$56,262")
    composition = page.get_by_role("region", name="Spending in this window")
    await expect(composition).to_be_visible()
    await expect(composition.get_by_text("$12,200", exact=True)).to_be_visible()

    history = page.get_by_role("region", name="Spending and funding history")
    canvas = history.get_by_role("img", name="Annual funding including taxes by day")
    await expect(canvas).to_be_visible()
    bounds = await canvas.bounding_box()
    assert bounds is not None
    await canvas.hover(position={"x": bounds["width"] / 2, "y": bounds["height"] / 2})
    tooltip = page.get_by_role("tooltip", name="History ending October 14, 2026")
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_contain_text("Provisional point: a reversal is unverified")

    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await view.capture(
        f"sustainability-provisional-reversal-{width}",
        label=f"{width}px provisional reversal estimate with retained funding and marked history",
        full_page=True,
        animations="disabled",
    )

    async def serve_historical_provisional(route: Route) -> None:
        historical = sustainability.view()
        latest = historical.history[-1]
        provisional_point = SustainabilityHistoryPoint(
            date=latest.date,
            status=ReportStatus.PROVISIONAL,
            covered_days=latest.covered_days,
            annualized_spending_minor_units=latest.annualized_spending_minor_units,
            spending_draw_rate=latest.spending_draw_rate,
            tax_adjusted_draw_rate=latest.tax_adjusted_draw_rate,
        )
        historical = sustainability.with_history(
            historical,
            [provisional_point],
            issues=[
                "Historical chart: Review the unverified reversal and confirm its classification "
                "before relying on this point."
            ],
        )
        await route.fulfill(json=sustainability.dashboard(historical).model_dump(mode="json"))

    await page.unroute("**/api/v1/sustainability/windows", serve_provisional)
    await page.route("**/api/v1/sustainability/windows", serve_historical_provisional)
    await page.reload(wait_until="domcontentloaded")
    await expect(page.get_by_text("Provisional", exact=True)).to_have_count(0)
    historical_issue = page.get_by_role("alert").filter(has_text="Historical chart: Review the unverified reversal")
    await expect(historical_issue).to_be_visible()
    await expect(page.get_by_role("region", name="Tax-adjusted implied draw rate")).to_contain_text("$56,262")
    history = page.get_by_role("region", name="Spending and funding history")
    canvas = history.get_by_role("img", name="Annual funding including taxes by day")
    await expect(canvas).to_be_visible()
    bounds = await canvas.bounding_box()
    assert bounds is not None
    await canvas.hover(position={"x": bounds["width"] / 2, "y": bounds["height"] / 2})
    tooltip = page.get_by_role("tooltip", name="History ending October 14, 2026")
    await expect(tooltip).to_contain_text("Provisional point: a reversal is unverified")
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    view.errors.assert_none(context="Provisional sustainability estimate")


@pytest.mark.parametrize("variant", ["stale", "unavailable"])
@pytest.mark.parametrize(("width", "height"), [(1280, 960), (390, 844)])
async def test_sustainability_stale_and_unavailable_render(
    page: Page, view: VisualPage, dashboard_url: str, variant: Literal["stale", "unavailable"], width: int, height: int
) -> None:
    async def serve_variant(route: Route) -> None:
        await route.fulfill(json=sustainability.dashboard(sustainability.view(variant)).model_dump(mode="json"))

    await page.route("**/api/v1/sustainability/windows", serve_variant)
    await page.set_viewport_size({"width": width, "height": height})
    await page.goto(dashboard_url, wait_until="domcontentloaded")
    await _select_spend_page(page, "Sustainability")
    await page.get_by_role("alert").get_by_text("Withdrawal rate unavailable", exact=False).wait_for()
    await expect(page.get_by_text("Portfolio used in the estimate", exact=True)).to_be_visible()
    await expect(page.get_by_text("Valuation unavailable", exact=True)).to_be_visible()
    await expect(page.get_by_text("ANNUALIZED SPENDING", exact=True)).to_be_visible()
    await expect(page.get_by_text("$49,511", exact=True)).to_be_visible()
    await expect(page.locator('[role="img"][aria-label*="gauge"]')).to_have_count(0)
    history = page.get_by_role("region", name="Spending and funding history")
    await expect(history.get_by_role("img", name="Annual funding including taxes by day")).to_be_visible()
    await expect(history.get_by_text("Portfolio value is unavailable or non-positive", exact=False)).to_be_visible()
    await expect(history.get_by_text("Reference withdrawal scenarios", exact=True)).to_have_count(0)
    await history.get_by_label("Include withdrawal taxes in dollar amounts").uncheck()
    await expect(history.get_by_role("img", name="Annual spending excluding taxes by day")).to_be_visible()
    await history.get_by_label("Include withdrawal taxes in dollar amounts").check()
    if width == 1280:
        canvas = history.get_by_role("img", name="Annual funding including taxes by day")
        bounds = await canvas.bounding_box()
        assert bounds is not None
        await canvas.hover(position={"x": bounds["width"] / 2, "y": bounds["height"] / 2})
        tooltip = page.get_by_role("tooltip", name="History ending October 13, 2026")
        await expect(tooltip).to_contain_text("Annual spending excluding taxes: $48,500")
        await expect(tooltip).to_contain_text("Annual funding including taxes: $55,114")
        await expect(tooltip).to_contain_text("Required withdrawal rate including taxes: unavailable")
        await page.mouse.move(0, 0)
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


async def test_accordion_readiness_ignores_unrelated_animations(page: Page, runfiles: Runfiles) -> None:
    await page.set_content(
        (runfiles.root() / "ducktape/finance/plaid/spend/ui/accordion-readiness-fixture.html").read_text()
    )
    await _expand_accordion(page.get_by_role("button", name="Expand"))
    await expect(page.locator("#panel")).to_have_css("height", "120px")
    assert await page.locator("#spinner").evaluate("element => element.getAnimations().length") == 1


if __name__ == "__main__":
    pytest_bazel.main()
