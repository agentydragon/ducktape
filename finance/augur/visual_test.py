"""Render-health checks + PR-visuals publication for representative Augur URLs.

Each case boots the real dev server (hermetic prices), drives the page through
its DOM/geometry assertions (the real regression net), and fails on
any uncaught page error. The rendered PNGs plus a `visual-review.json` manifest
go to undeclared outputs, where trusted CI (`devinfra/pr_visuals/publisher.py`)
publishes them for review.

There is no checked-in pixel golden — pixel changes are reviewed on the PR's
visual-review page, not gated in CI (see
devinfra/pr_visuals/plans/goldens_to_pr_visuals.md).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlencode

import pytest
import pytest_bazel
from playwright.async_api import Page, Playwright, ViewportSize

from finance.augur.api.config import Config
from finance.augur.api.server import static_price_clients
from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.calibration.testing import mock_price_clients
from finance.augur.dev_server import build_dev_app
from finance.evidence.markets import Platform
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual import deterministic_browser_context, stability_style
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

# pytest_plugins loads util.playwright by name; gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
# gazelle:include_dep //util/testing:visual_fixtures

pytest_plugins = ("util.playwright", "util.testing.visual_fixtures")


SCREENSHOT_VIEWPORT: ViewportSize = {"width": 1280, "height": 1000}
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.


async def _wait_for_product_chart_geometry(page: Page) -> None:
    """Wait for ResizeObserver-fed chart coordinates to catch up with the visible SVG width."""
    await page.wait_for_function(
        """
        () => {
          const chart = document.querySelector("[data-product-fan-chart='netWorthQuanta'] svg[role='img']");
          if (!chart) return false;
          // Horizon is now driven by the wheel/`?h=`, not an input. Use the chart's own rightmost
          // "N yr" tick as the final-year marker whose geometry must have settled within bounds.
          const yearTicks = Array.from(chart.querySelectorAll("text")).filter((node) =>
            /^\\d+ yr$/.test(node.textContent.trim())
          );
          if (yearTicks.length === 0) return false;
          const finalYearTick = yearTicks.reduce((a, b) =>
            parseInt(a.textContent, 10) >= parseInt(b.textContent, 10) ? a : b
          );
          const tickBox = finalYearTick.getBoundingClientRect();
          const chartBox = chart.getBoundingClientRect();
          return (
            tickBox.left >= chartBox.left - 1 &&
            tickBox.right <= chartBox.right + 1 &&
            tickBox.left >= 0 &&
            tickBox.right <= window.innerWidth + 1
          );
        }
        """,
        timeout=30_000,
    )


async def _wait_for_terminal_distribution_density(page: Page, *, min_series: int) -> None:
    """Wait until the terminal-distribution chart is drawing dense terminal percentiles."""
    await page.wait_for_function(
        f"""
        () => {{
          const plot = document.querySelector("[data-product-terminal-distribution-plot]");
          const series = Array.from(document.querySelectorAll("[data-product-distribution-series]"));
          if (!plot) return false;
          const renderedWidth = Number(plot.getAttribute("data-product-terminal-distribution-rendered-width"));
          const boxWidth = plot.getBoundingClientRect().width;
          return (
            Number.isFinite(renderedWidth) &&
            renderedWidth >= boxWidth - 2 &&
            series.length >= {min_series} &&
            series.every((node) => Number(node.getAttribute("data-product-distribution-point-count")) >= 101)
          );
        }}
        """,
        timeout=30_000,
    )


async def _click_terminal_distribution_percentile(page: Page, *, percentile: float, y_fraction: float) -> None:
    plot = page.locator("[data-product-terminal-distribution-plot]")
    await plot.wait_for(state="visible", timeout=30_000)
    box = await plot.bounding_box()
    assert box is not None
    x = await page.evaluate(
        """
        (percentile) => {
          const plot = document.querySelector("[data-product-terminal-distribution-plot]");
          const renderedWidth =
            Number(plot.getAttribute("data-product-terminal-distribution-rendered-width")) ||
            plot.getBoundingClientRect().width;
          const marginLeft = 82;
          const marginRight = 20;
          return marginLeft + Math.max(0, Math.min(1, percentile)) * Math.max(1, renderedWidth - marginLeft - marginRight);
        }
        """,
        percentile,
    )
    await plot.click(position={"x": float(x), "y": box["height"] * y_fraction})


async def _wait_for_product_page(page: Page) -> None:
    """Wait for the product surface's net-worth fan to render at non-zero height."""
    await page.add_style_tag(content=stability_style())
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-fan-chart='netWorthQuanta']").wait_for(state="visible", timeout=30_000)
    await page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=30_000)
    await page.get_by_label("Metric to plot").wait_for(state="visible", timeout=30_000)
    await page.wait_for_function(
        """
        () => {
          const chart = document.querySelector("[data-product-fan-chart='netWorthQuanta'] svg[role='img']");
          if (!chart) return false;
          const heights = Array.from(chart.querySelectorAll("polygon")).map((polygon) => {
            const points = (polygon.getAttribute("points") || "")
              .trim()
              .split(/\\s+/)
              .map((point) => Number(point.split(",")[1]))
              .filter(Number.isFinite);
            return points.length ? Math.max(...points) - Math.min(...points) : 0;
          });
          return Math.max(0, ...heights) >= 80;
        }
        """,
        timeout=30_000,
    )
    assert await page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    await page.evaluate("() => document.fonts.ready.then(() => true)")
    await _wait_for_product_chart_geometry(page)
    await _wait_for_terminal_distribution_density(page, min_series=1)


async def _select_first_rollout(page: Page) -> None:
    """Select a rollout from the terminal-distribution chart and exercise the marker↔event-table
    cross-selection handshake. A click anywhere in the plot selects the nearest variant line at that
    percentile; clicking at 70% width binds to a mid-upper rollout. Leaves the table-clicked-month
    selected so the screenshot shows event detail."""
    await _click_terminal_distribution_percentile(page, percentile=0.7, y_fraction=0.5)
    await page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-rollout-event-marker]").first.wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)
    await page.locator(r"text=/Seed \d+ - (completed|failed m\d+)/").wait_for(state="visible", timeout=30_000)
    marker = page.locator("[data-product-rollout-event-marker]").last
    marker_month = await marker.get_attribute("data-product-rollout-event-marker-month")
    assert marker_month is not None
    await marker.click()
    await page.locator(
        f"[data-product-rollout-event-month='{marker_month}'][data-product-rollout-event-month-selected='true']"
    ).wait_for(state="visible", timeout=30_000)
    await marker.click()
    await page.locator(
        f"[data-product-rollout-event-month='{marker_month}'][data-product-rollout-event-month-selected='false']"
    ).wait_for(state="visible", timeout=30_000)
    await page.locator(
        f"[data-product-rollout-event-marker-month='{marker_month}'][data-product-rollout-event-marker-selected='false']"
    ).first.wait_for(state="visible", timeout=30_000)
    # Pick a table-row month that has a corresponding marker — `monthly_expense` and `outside_rent`
    # have no markers, so the first table row may be a marker-less month.
    first_marker_month = await page.locator("[data-product-rollout-event-marker]").first.get_attribute(
        "data-product-rollout-event-marker-month"
    )
    assert first_marker_month is not None
    table_group = page.locator(f"[data-product-rollout-event-month='{first_marker_month}']")
    table_month = first_marker_month
    await table_group.click()
    await page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='true']"
    ).first.wait_for(state="visible", timeout=30_000)
    await table_group.click()
    await page.locator(
        f"[data-product-rollout-event-month='{table_month}'][data-product-rollout-event-month-selected='false']"
    ).wait_for(state="visible", timeout=30_000)
    await page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='false']"
    ).first.wait_for(state="visible", timeout=30_000)
    await table_group.click()
    await page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='true']"
    ).first.wait_for(state="visible", timeout=30_000)


async def _wait_for_property_panel(page: Page) -> None:
    """Wait for the Base owning rows + the lifecycle timeline editor (prefilled events) to mount."""
    await _wait_for_product_page(page)
    # Owning knobs surface as table rows once the (single) Base scenario buys; the lifecycle timeline
    # is now one of those scenario table rows.
    await page.locator("[data-product-knob-row='financingKind']").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-timeline]").wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Timeline (mid-horizon changes)").wait_for(state="visible", timeout=30_000)
    # Three event rows pre-decoded from the URL: set-rented%, capital improvement, sale.
    await page.get_by_label("Rented", exact=True).wait_for(state="visible", timeout=30_000)
    await page.get_by_label("Amount", exact=True).wait_for(state="visible", timeout=30_000)
    await page.get_by_label("Closing cost", exact=True).wait_for(state="visible", timeout=30_000)


async def _wait_for_distribution_failures(page: Page) -> None:
    """Inspect a stopped book without placing it in the terminal-wealth distribution."""
    await page.add_style_tag(content=stability_style())
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-fan-chart='netWorthQuanta']").wait_for(state="visible", timeout=30_000)
    await _wait_for_terminal_distribution_density(page, min_series=1)
    stopped_selector = page.get_by_label("Inspect stopped rollout", exact=True)
    await stopped_selector.wait_for(state="visible", timeout=30_000)
    seed = await stopped_selector.locator("option").nth(1).get_attribute("value")
    assert seed is not None
    await stopped_selector.select_option(seed)
    await page.locator("[data-product-stop-book]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    assert await page.locator("[data-product-distribution-failed]").count() == 0
    assert await page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    await page.evaluate("() => document.fonts.ready.then(() => true)")
    await _wait_for_product_chart_geometry(page)


async def _wait_for_calibration_page(page: Page) -> None:
    """Wait for the calibration tab's auto-run to land (results, not just the form).

    The tab now auto-runs on load (no button), so the screenshot captures the scored-markets
    table and the issuer mark fan. Hermetic prices are served by the in-process server, so the
    auto-run resolves without touching the network."""
    await page.add_style_tag(content=stability_style())
    await page.locator("[data-augur-surface='calibration']").wait_for(state="visible", timeout=30_000)
    await page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=30_000)
    await page.locator("[data-augur-tab='calibration'][data-active]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-calibration-catalog]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-calibration-categorical-chart]").first.wait_for(state="visible", timeout=30_000)
    await page.locator("[data-calibration-mark-fan]").wait_for(state="visible", timeout=30_000)
    assert await page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    await page.evaluate("() => document.fonts.ready.then(() => true)")


# A single Base scenario that buys the fixture property `location_a_property` and carries three
# mid-horizon lifecycle events: set rented to 50% at month 24, a $50k capital improvement at month
# 60, and a sale at month 120 with 6% closing cost. The horizon (240 months) rides the tab-shared
# `?h=` control; every other knob inherits `productInputDefaults`. Encoded in the same `?scenarios=`
# base+overrides codec (v3) as the comparison case, just with no variants.
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
# Rendering controls use a bounded real population, independent of the UI's sampling default.
_PROPERTY_LIFECYCLE_URL = "/product?" + urlencode(
    {"scenarios": json.dumps(_PROPERTY_LIFECYCLE_SCENARIOS), "h": "240", "n": "32"}
)

# Three-scenario "rent vs. buy A vs. buy B" comparison in the base+overrides codec (v3). The Base
# scenario "Rent" sets only the fields that differ from the product defaults (the codec merges the
# rest over `productInputDefaults`); each variant buys a different fixture property and stops paying
# outside rent. So the editor spreadsheet shows three columns with a per-scenario "Property to buy"
# row (none / Location A / Location B) and an overridden "Monthly rent" row ($3,000 / $0 / $0) — a
# mix of Base, inherited (muted), and overridden (bold + ↩) cells. The whole set rides the
# URL-encoded `?scenarios=` param.
_COMPARISON_SCENARIOS = {
    "v": 3,
    "base": {"label": "Rent", "input": {"monthlyRent": 3000}},
    "variants": [
        {
            "label": "Buy A",
            "overrides": {
                "propertyId": "location_a_property",
                "financingKind": "mortgage",
                "livesHere": True,
                "monthlyRent": 0,
            },
        },
        {
            "label": "Buy B",
            "overrides": {
                "propertyId": "location_b_property",
                "financingKind": "mortgage",
                "livesHere": True,
                "monthlyRent": 0,
            },
        },
    ],
}
_COMPARISON_URL = "/product?" + urlencode({"scenarios": json.dumps(_COMPARISON_SCENARIOS), "h": "240", "n": "32"})

# A single Base scenario engineered to bust a chunk of its rollouts: a high monthly spend against the
# fixture portfolio so weaker-market paths exhaust cash and holdings before the 10y horizon, while
# stronger-market paths survive. It exercises direct stopped-book inspection while terminal
# wealth contains completed paths only. `cashCeiling` is raised so each crossing of the floor refills a chunk large
# enough to keep funding ahead of spend — a bust then means holdings genuinely ran out, which is
# market-path-dependent (hence partial). The target allocation is left unset, so it seeds from the
# fixture holdings and every sellable position can fund the band.
_FAILURE_SCENARIOS = {
    "v": 3,
    "base": {"label": "Aggressive drawdown", "input": {"monthlySpend": 9000, "cashCeiling": 40000}},
    "variants": [],
}
_FAILURE_URL = "/product?" + urlencode({"scenarios": json.dumps(_FAILURE_SCENARIOS), "h": "120", "n": "32"})


async def _wait_for_scenario_comparison(page: Page) -> None:
    """Wait for the multi-scenario overlay: the scenario bar, the editor spreadsheet with a Base +
    two variant columns (and the per-scenario "Property to buy" row), three scenario fans + legend,
    and the per-scenario comparison table."""
    await page.add_style_tag(content=stability_style())
    await page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-scenario-tabs]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-fan-chart='netWorthQuanta']").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-fan-legend]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-scenario-comparison]").wait_for(state="visible", timeout=30_000)
    # The editor spreadsheet shows Base + the two variants as columns (rows = knobs), including the
    # per-scenario property row.
    await page.locator("[data-product-scenario-table]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-knob-row='propertyId']").wait_for(state="visible", timeout=30_000)
    await page.wait_for_function(
        '() => document.querySelectorAll("[data-product-scenario-col]").length >= 3', timeout=30_000
    )
    # All three scenario fans have drawn their median lines.
    await page.wait_for_function(
        '() => document.querySelectorAll("[data-product-fan-series]").length >= 3', timeout=30_000
    )
    # The terminal-distribution chart overlays one dense line per variant (all three present).
    await _wait_for_terminal_distribution_density(page, min_series=3)
    assert await page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    await page.evaluate("() => document.fonts.ready.then(() => true)")
    await _wait_for_product_chart_geometry(page)


async def _show_candles(page: Page) -> None:
    """Switch the rollout chart from fans to candles, then select a rollout so the screenshot pins
    the continuous rollout trajectory + event markers over the box-and-whisker candles."""
    await page.locator("[data-product-chart-mode-toggle]").get_by_text("Candles", exact=True).click()
    await page.locator("[data-product-candle-series]").first.wait_for(state="visible", timeout=30_000)
    await page.wait_for_function(
        '() => new Set([...document.querySelectorAll("[data-product-candle-series]")]'
        '.map((node) => node.getAttribute("data-product-candle-series"))).size >= 3',
        timeout=30_000,
    )
    await _select_rollout_from_distribution(page)
    await page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-rollout-event-marker]").first.wait_for(state="visible", timeout=30_000)


async def _select_rollout_from_distribution(page: Page) -> None:
    """Select a rollout directly from the multi-variant terminal-distribution chart. A click binds to
    the nearest variant line at that percentile (making it active), drawing the selection marker on
    the line and the rollout overlay on the timeline chart below, with the events panel carrying the active
    badge."""
    await _click_terminal_distribution_percentile(page, percentile=0.6, y_fraction=0.4)
    await page.locator("[data-product-distribution-selected]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    await page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)


async def _focus_active_scenario(page: Page) -> None:
    """Collapse the multi-scenario overlay to just the active scenario via the Compare/Focus toggle,
    then select a rollout. Focus renders the active variant as a full single-scenario fan (the
    pre-comparison view), so the multi-scenario legend disappears; with a rollout selected, the
    events panel below carries the active-scenario badge that names which variant the timeline is."""
    await page.locator("[data-product-fan-legend]").wait_for(state="visible", timeout=30_000)
    await page.locator("[data-product-scenario-focus-toggle]").get_by_text("Focus", exact=True).click()
    await page.locator("[data-product-fan-legend]").wait_for(state="detached", timeout=30_000)
    await _select_first_rollout(page)


@pytest.fixture(scope="module")
def hermetic_prices() -> dict[Platform, dict[str, float]]:
    """A fixed live price for every market in the example catalog.

    The calibration tab auto-runs on load and scores every catalog market, so the in-process
    server needs a probability for each market to resolve the run with no network. The exact
    values only need to be plausible and deterministic; a gentle spread keeps the scored table
    and surfaced list visually populated."""
    catalog = MarketCatalog.from_yaml(get_required_path("_main/finance/augur/calibration/example_openai_catalog.yaml"))
    by_platform: defaultdict[Platform, dict[str, float]] = defaultdict(dict)
    for index, market in enumerate(catalog.markets):
        by_platform[market.platform][market.market_id] = 0.3 + 0.4 * (index % 3) / 2
    # Bucket families live outside `markets`; price each member so the categorical auto-run resolves.
    for bucket_family in catalog.bucket_families:
        for index, bucket_member in enumerate(bucket_family.buckets):
            by_platform[bucket_family.platform][bucket_member.market_id] = 0.2 + 0.6 * (index % 4) / 3
    for threshold_family in catalog.threshold_ladder_families:
        for index, threshold_member in enumerate(threshold_family.thresholds):
            by_platform[threshold_family.platform][threshold_member.market_id] = 0.2 + 0.6 * (index % 4) / 3
    for date_family in catalog.date_ladder_families:
        for index, date_member in enumerate(date_family.dates):
            by_platform[date_family.platform][date_member.market_id] = 0.2 + 0.6 * (index % 4) / 3
    return dict(by_platform)


@pytest.fixture(scope="module")
def augur_server(augur_config: Config, hermetic_prices: dict[Platform, dict[str, float]]) -> Iterator[str]:
    # Inject hermetic mock clients so the calibration tab's auto-run never hits the network.
    app = build_dev_app(augur_config, price_clients=static_price_clients(mock_price_clients(hermetic_prices)))
    with serve_app_sync(app) as base_url:
        yield base_url


@pytest.fixture
async def page(playwright: Playwright) -> AsyncIterator[Page]:
    async with deterministic_browser_context(
        playwright, viewport=SCREENSHOT_VIEWPORT, frozen_now_ms=FROZEN_NOW_MS
    ) as context:
        page = await context.new_page()

        yield page


@pytest.fixture
async def view(page: Page, capture_name: str) -> AsyncIterator[VisualPage]:
    view = VisualPage(
        page, output_dir=undeclared_outputs_dir(), title="Augur pages", capture_name=capture_name, output_suffix=""
    )
    try:
        yield view
    finally:
        # Readiness failures still leave a diagnostic screenshot and DOM.
        if not (view.output_dir / f"{capture_name}.png").exists():
            await page.screenshot(path=str(view.output_dir / f"{capture_name}.debug.png"), full_page=True)
            (view.output_dir / f"{capture_name}.debug.html").write_text((await page.content())[:5000])
        view.errors.assert_none(context=capture_name)


@pytest.fixture
async def comparison_view(view: VisualPage, augur_server: str) -> VisualPage:
    await view.page.goto(f"{augur_server}{_COMPARISON_URL}", wait_until="networkidle", timeout=60_000)
    await _wait_for_scenario_comparison(view.page)
    return view


async def test_product_cash_runway(view: VisualPage, augur_server: str) -> None:
    await view.page.goto(f"{augur_server}/product?n=32", wait_until="networkidle", timeout=60_000)
    await _wait_for_product_page(view.page)
    await _select_first_rollout(view.page)
    await _wait_for_product_chart_geometry(view.page)
    await view.page.evaluate("() => window.scrollTo(0, 0)")
    await view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_property_lifecycle(view: VisualPage, augur_server: str) -> None:
    await view.page.goto(f"{augur_server}{_PROPERTY_LIFECYCLE_URL}", wait_until="networkidle", timeout=60_000)
    await _wait_for_property_panel(view.page)
    await view.page.evaluate("() => window.scrollTo(0, 0)")
    await view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_scenario_comparison(comparison_view: VisualPage) -> None:
    await comparison_view.page.evaluate("() => window.scrollTo(0, 0)")
    await comparison_view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_distribution_multi(comparison_view: VisualPage) -> None:
    await _select_rollout_from_distribution(comparison_view.page)
    await _wait_for_product_chart_geometry(comparison_view.page)
    await comparison_view.page.evaluate("() => window.scrollTo(0, 0)")
    await comparison_view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_scenario_candles(comparison_view: VisualPage) -> None:
    await _show_candles(comparison_view.page)
    await _wait_for_product_chart_geometry(comparison_view.page)
    await comparison_view.page.evaluate("() => window.scrollTo(0, 0)")
    await comparison_view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_scenario_focus(comparison_view: VisualPage) -> None:
    await _focus_active_scenario(comparison_view.page)
    await _wait_for_product_chart_geometry(comparison_view.page)
    await comparison_view.page.evaluate("() => window.scrollTo(0, 0)")
    await comparison_view.capture(full_page=True, animations="disabled", scale="css")


async def test_product_distribution_failures(view: VisualPage, augur_server: str) -> None:
    await view.page.goto(f"{augur_server}{_FAILURE_URL}", wait_until="networkidle", timeout=60_000)
    await _wait_for_distribution_failures(view.page)
    await view.page.evaluate("() => window.scrollTo(0, 0)")
    await view.capture(full_page=True, animations="disabled", scale="css")


async def test_calibration_page(view: VisualPage, augur_server: str) -> None:
    await view.page.goto(f"{augur_server}/product?tab=calibration", wait_until="networkidle", timeout=60_000)
    await _wait_for_calibration_page(view.page)
    await view.page.evaluate("() => window.scrollTo(0, 0)")
    await view.capture(full_page=True, animations="disabled", scale="css")


if __name__ == "__main__":
    pytest_bazel.main()
