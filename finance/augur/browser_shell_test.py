"""Public Augur product-surface browser smoke test against the Bazel-runnable server."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
import pytest_bazel
from playwright.async_api import Browser, Page

from finance.augur.testing.server import run_server
from util.bazel.runfiles import get_required_path

# pytest_plugins loads util.playwright by name; gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)

# A single Base scenario that buys the fixture property `location_a_property` (so the recurring
# property-expense events — tax, insurance, maintenance — fire) plus three mid-horizon lifecycle
# events. Encoded in the `?scenarios=` base+overrides codec (v3); the horizon rides the tab-shared
# `?h=` control and every other knob inherits `productInputDefaults`.
_PROPERTY_LIFECYCLE_SCENARIOS = {
    "v": 3,
    "base": {
        "label": "Base",
        "input": {
            "propertyId": "location_a_property",
            "propertyLifecycleEvents": [
                {"kind": "set_rented_fraction", "month": 24, "rentedFractionPct": 50},
                {"kind": "capital_improvement", "month": 60, "amount": 50000},
                {"kind": "property_sale", "month": 120, "closingCostPct": 6},
            ],
        },
    },
    "variants": [],
}
# Exercise real trajectories without making browser interaction checks a throughput gate.
_PROPERTY_LIFECYCLE_URL = "/product?" + urlencode(
    {"scenarios": json.dumps(_PROPERTY_LIFECYCLE_SCENARIOS), "h": "240", "n": "32"}
)


# The shared `browser` launches the container base flags only, not the deterministic set the
# visual tests add: this test drives the shell and reads the DOM, and never captures a pixel.
@pytest.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    async with await browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        yield await context.new_page()


@pytest.fixture
def page_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


@pytest.fixture
def augur_server(tmp_path: Path) -> Iterator[str]:
    with run_server(binary=get_required_path("_main/finance/augur/dev"), tmp_path=tmp_path) as origin:
        yield origin


async def test_product_shell_renders_metric_fan_charts(page: Page, page_errors: list[str], augur_server: str) -> None:
    """Smoke-test the product surface end-to-end: load `/product`, select a few metrics,
    confirm the matching fan chart renders for each."""
    await page.goto(f"{augur_server}/product?n=32", wait_until="domcontentloaded")
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    assert not page_errors
    await page.locator("[data-product-results-ready]").wait_for(state="visible", timeout=30_000)
    await page.get_by_label("Metric to plot").wait_for(state="visible", timeout=15_000)
    await page.get_by_label("Metric to plot").select_option("cash")
    await page.locator("[data-product-fan-chart='cashQuanta']").wait_for(state="visible", timeout=30_000)
    # The linear/log scale toggle is in the sidebar's SharedControls.
    await page.get_by_label("Chart scale").get_by_text("Log", exact=True).click()
    await page.locator("[data-product-fan-chart='cashQuanta'][data-product-scale='log']").wait_for(
        state="visible", timeout=15_000
    )
    await page.locator("[data-product-distribution-scale='log']").wait_for(state="visible", timeout=15_000)
    await page.get_by_label("Chart scale").get_by_text("Linear", exact=True).click()
    await page.locator("[data-product-fan-chart='cashQuanta'][data-product-scale='linear']").wait_for(
        state="visible", timeout=15_000
    )
    await page.locator("[data-product-distribution-scale='linear']").wait_for(state="visible", timeout=15_000)
    await page.get_by_label("Metric to plot").select_option("holding_value")
    await page.locator("[data-product-fan-chart='holdingValueQuanta']").wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Initial portfolio").wait_for(state="visible", timeout=15_000)
    # Grand total (cash + holdings + bond face) is shown in the collapsed accordion summary.
    assert await page.locator("[data-product-portfolio-subtotal='total']").inner_text() == "USD\u00a01,260,500.00"
    # Open the accordion to see per-bucket subtotals inline with their positions.
    await page.get_by_text("Initial portfolio").click()
    assert (
        await page.locator("[data-product-portfolio-subtotal='public-securities']").inner_text()
        == "USD\u00a0835,500.00"
    )
    assert (
        await page.locator("[data-product-portfolio-subtotal='private-securities']").inner_text()
        == "USD\u00a025,000.00"
    )
    # The ladder gets its own group rather than a row in either of those: face is not a mark, so
    # folding it into a "value" subtotal would assert a price the model does not produce.
    assert await page.locator("[data-product-portfolio-subtotal='bonds']").inner_text() == "USD\u00a0150,000.00"
    assert await page.get_by_text("Bonds (held to maturity)").is_visible()


async def test_property_recurring_expense_events_start_hidden_on_rollout_graph(page: Page, augur_server: str) -> None:
    await page.goto(f"{augur_server}{_PROPERTY_LIFECYCLE_URL}", wait_until="domcontentloaded")
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    await page.locator("[data-product-fan-chart='netWorthQuanta']").wait_for(state="visible", timeout=30_000)
    # Select a rollout by clicking the terminal-distribution plot (the per-rollout "sliver" strip
    # was replaced by the quantile-line distribution in #1848; the plot is the click-to-inspect surface).
    plot = page.locator("[data-product-terminal-distribution-plot]")
    await plot.wait_for(state="visible", timeout=30_000)
    box = await plot.bounding_box()
    assert box is not None
    await plot.click(position={"x": box["width"] * 0.6, "y": box["height"] * 0.5})
    await page.locator("[data-product-distribution-selected]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Event kinds").wait_for(state="visible", timeout=30_000)
    legend = page.get_by_label("Event-kind visibility legend")

    for label, event_kind in [
        ("Property tax", "property_tax_payment"),
        ("Homeowners insurance", "homeowners_insurance_payment"),
        ("Maintenance", "property_maintenance_payment"),
    ]:
        button = legend.get_by_role("button", name=re.compile(label))
        await button.wait_for(state="visible", timeout=15_000)
        assert await button.get_attribute("aria-pressed") == "false"
        assert await page.locator(f"[data-product-rollout-event-marker='{event_kind}']").count() == 0


async def test_distribution_click_away_clears_selection(page: Page, augur_server: str) -> None:
    """Selecting a rollout from the distribution chart and then clicking well clear of the line
    clears the selection (click-away-to-deselect)."""
    await page.goto(f"{augur_server}{_PROPERTY_LIFECYCLE_URL}", wait_until="domcontentloaded")
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    await page.locator("[data-product-fan-chart='netWorthQuanta']").wait_for(state="visible", timeout=30_000)
    plot = page.locator("[data-product-terminal-distribution-plot]")
    await plot.wait_for(state="visible", timeout=30_000)
    box = await plot.bounding_box()
    assert box is not None
    # A first click selects the nearest rollout regardless of distance — the marker appears.
    await plot.click(position={"x": box["width"] * 0.6, "y": box["height"] * 0.5})
    await page.locator("[data-product-distribution-selected]").wait_for(state="visible", timeout=30_000)
    # A click in the empty top-left (far above the rising net-worth curve) clears the selection.
    await plot.click(position={"x": box["width"] * 0.05, "y": box["height"] * 0.06})
    await page.locator("[data-product-distribution-selected]").wait_for(state="detached", timeout=30_000)


if __name__ == "__main__":
    pytest_bazel.main()
