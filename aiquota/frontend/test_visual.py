"""Dashboard fixtures across themes, plus the two narrow layout cases."""

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import expect

from util.bazel.runfiles import get_required_path
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness, VisualPage

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

_FIXTURES = json.loads(get_required_path(os.environ["FIXTURE_CATALOG"]).read_bytes())
_SCENES = tuple(_FIXTURES)
if not _SCENES:
    raise ValueError("the dashboard fixture catalog is empty")


@pytest_asyncio.fixture(loop_scope="session")
async def view(
    visual: VisualHarness, scene: str, color_scheme: Literal["light", "dark"], viewport: Viewport, capture_name: str
) -> AsyncIterator[VisualPage]:
    async with visual.open(
        query={"scene": scene}, viewport=viewport, color_scheme=color_scheme, capture_name=capture_name
    ) as view:
        yield view


@pytest.mark.parametrize(
    ("scene", "color_scheme", "viewport"),
    [
        pytest.param(scene, scheme, Viewport(width=1200, height=900, device_scale_factor=2), id=f"{scene}-{scheme}")
        for scene in _SCENES
        for scheme in ("light", "dark")
    ]
    + [
        pytest.param(scene, "dark", Viewport(width=420, height=900, device_scale_factor=2), id=f"{scene}-dark-narrow")
        for scene in ("hot", "paid_credits")
    ],
)
async def test_dashboard(view: VisualPage, scene: str) -> None:
    await expect(view.page.locator(".aiquota-card").first).to_be_attached()
    await expect(view.page.get_by_role("status")).to_have_text("Quota stream live")
    if scene == "reset_credit_expiry_unavailable":
        await expect(view.page.locator(".aiquota-strip-resets .aiquota-strip-line")).to_contain_text("2 banked resets")
        await expect(view.page.get_by_text("expiry dates unavailable", exact=True)).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


async def test_live_subscription(visual: VisualHarness) -> None:
    async with visual.open(
        query={"scene": "hot", "live": "1"},
        frozen_now_ms=int(datetime.fromisoformat(_FIXTURES["hot"]["fetched_at"]).timestamp() * 1000),
    ) as view:
        page = view.page
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await expect(page.get_by_text("Loading quota data…")).to_be_visible()
        await expect(page.get_by_role("status")).to_contain_text("Connecting")
        await expect(page.get_by_role("button", name="Reconnect")).to_be_disabled()
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["hot"],
        )
        await expect(page.locator(".aiquota-card").first).to_be_visible()
        await expect(page.get_by_role("button", name="Reconnect")).to_be_enabled()
        await expect(page.get_by_role("status")).to_have_text("Quota stream live")
        await page.evaluate("window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: '{}'}))")
        await expect(page.get_by_role("status")).to_have_text("Quota stream unavailable")
        await expect(page.locator(".aiquota-card").first).to_be_visible()
        await view.capture("live-invalid-update", target=page.locator("#app"))
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["hot"],
        )
        await expect(page.get_by_role("status")).to_have_text("Quota stream live")
        await page.evaluate("window.dispatchEvent(new Event('quota-fixture-error'))")
        await expect(page.get_by_text("Connection interrupted. Reconnecting to live updates…")).to_be_visible()
        await expect(page.get_by_role("status")).to_have_text("Quota stream unavailable")
        await expect(page.locator(".aiquota-card").first).to_be_visible()
        await view.capture("live-reconnecting", target=page.locator("#app"))
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["paid_credits"],
        )
        await expect(page.locator(".aiquota-notice")).to_have_count(0)
        await expect(page.get_by_text("credits left", exact=False).first).to_be_visible()
        await view.capture("live-recovered", target=page.locator("#app"))
        await page.get_by_role("button", name="Reconnect").click()
        await expect(page.get_by_role("button", name="Reconnect")).to_be_disabled()
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["hot"],
        )
        await expect(page.get_by_role("button", name="Reconnect")).to_be_enabled()
        await expect(page.get_by_role("status")).to_have_text("Quota stream live")


if __name__ == "__main__":
    pytest_bazel.main()
