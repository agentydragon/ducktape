"""Render the decision-first Spend page with synthetic allowance data.

Screenshot artifacts are published for PR review; this does not exercise private
login or assert that a deployed authenticated browser looks the same.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Page

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

    @app.get("/api/v1/web/view")
    def view() -> dict:
        return {
            "generated_at": "2026-10-15T12:00:00Z",
            "allowance": {
                "status": "active",
                "note": None,
                "currency": "USD",
                "alert_state": "normal",
                "monthly_minor_units": 70000,
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

    @app.get("/api/v1/web/events")
    async def events() -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            yield f"event: view\ndata: {json.dumps(view())}\n\n"
            while True:
                await asyncio.sleep(60)
                yield ": keepalive\n\n"

        return StreamingResponse(updates(), media_type="text/event-stream")

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
    await page.get_by_text("$200.00", exact=True).wait_for()
    assert await page.get_by_role("heading", name="Flexible spending", level=1).count() == 1
    assert await page.get_by_role("alert").get_by_text("2 charges ($15.00) need review").count() == 1
    assert await page.get_by_role("heading", name="Can I afford this?").count() == 1
    assert await page.get_by_text("$75.00", exact=True).count() == 1
    assert await page.get_by_text("since first recorded transaction", exact=False).count() == 1
    assert not errors
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.get_by_label("Hypothetical flexible purchase").fill("250")
    await page.get_by_text("Over allowance", exact=True).last.wait_for()
    assert await page.get_by_text("-$50.00", exact=True).count() == 1
    assert await page.get_by_text("$175.00 short before", exact=False).count() == 1
    assert not errors
    exceeded = tmp_path / f"dashboard-{width}-purchase.png"
    await page.screenshot(path=str(exceeded), full_page=True, animations="disabled")
    retain_review_asset(exceeded, title="Spend decisions", label=f"{width}px hypothetical purchase", name=exceeded.name)


if __name__ == "__main__":
    pytest_bazel.main()
