"""Public Augur product-surface browser smoke test against the Bazel-runnable server."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
import pytest_bazel
from playwright.async_api import Browser, Page

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

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
    out = undeclared_outputs_dir()
    server_log = (out / "augur-server.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/finance/augur/dev")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/finance/augur/api/testdata/config.yaml")),
        ],
        env={
            **os.environ,
            # An (empty) evidence checkout: the price readers boot against it and
            # would report any market as not-mirrored; this smoke test never reads one.
            "AUGUR_EVIDENCE_DIR": str(tmp_path / "evidence"),
            "HOME": str(tmp_path / "home"),
            "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
            "PYTHONUNBUFFERED": "1",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        stdout=server_log,
        stderr=server_log,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"Augur server exited early with code {server.returncode}; see {server_log.name}")
            try:
                with urllib.request.urlopen(f"{origin}/healthz", timeout=1) as response:
                    if response.status == 200 and response.read().decode() == "ok\n":
                        break
            except OSError, urllib.error.URLError:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Augur server did not start within 30s; see {server_log.name}")
        yield origin
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server_log.close()


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
