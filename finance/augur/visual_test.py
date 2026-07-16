"""Full-page visual goldens for representative public Augur URLs.

Update flow for intentional frontend changes:

    nix develop --command bazelisk test //finance/augur:visual_test \\
        --test_env=UPDATE_GOLDEN=1 \\
        --remote_upload_local_results=false --nocache_test_results

Then copy the produced PNGs from the test's undeclared outputs into
`augur/frontend/__screenshots__/` and rerun this test without `UPDATE_GOLDEN`.
With BuildBuddy/RBE, use the invocation id printed by Bazel:

    bbapi artifact download "$INV" "test.outputs/product_cash_runway.png" \\
        -o augur/frontend/__screenshots__/product_cash_runway.png
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest
import pytest_bazel
import uvicorn

from finance.augur.api.config import Config
from finance.augur.api.server import static_price_clients
from finance.augur.calibration.catalog import MarketCatalog
from finance.augur.calibration.testing import mock_price_clients
from finance.augur.dev_server import build_dev_app
from finance.evidence.markets import Platform
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port, wait_for_port
from util.testing.frontend_visual import (
    deterministic_browser_context,
    deterministic_style,
    launch_deterministic_browser,
)
from util.testing.png_diff import assert_png_matches_golden
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_review import retain_review_asset

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright, ViewportSize


@dataclass(frozen=True)
class VisualCase:
    name: str
    path: str
    wait_ready: Callable[[Page], None]
    # Optional interaction to run after the page is ready but before screenshot (e.g. clicking a
    # rollout sliver to expand the events panel). Mutating callable; receives the live `Page`.
    interact: Callable[[Page], None] | None = field(default=None)


SCREENSHOT_VIEWPORT: ViewportSize = {"width": 1280, "height": 1000}
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.


def _wait_for_product_chart_geometry(page: Page) -> None:
    """Wait for ResizeObserver-fed chart coordinates to catch up with the visible SVG width."""
    page.wait_for_function(
        """
        () => {
          const chart = document.querySelector("[data-product-fan-chart='netWorthUsd'] svg[role='img']");
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


def _wait_for_terminal_distribution_density(page: Page, *, min_series: int) -> None:
    """Wait until the terminal-distribution chart is drawing dense terminal percentiles."""
    page.wait_for_function(
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


def _click_terminal_distribution_percentile(page: Page, *, percentile: float, y_fraction: float) -> None:
    plot = page.locator("[data-product-terminal-distribution-plot]")
    plot.wait_for(state="visible", timeout=30_000)
    box = plot.bounding_box()
    assert box is not None
    x = page.evaluate(
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
    plot.click(position={"x": float(x), "y": box["height"] * y_fraction})


def _wait_for_product_page(page: Page) -> None:
    """Wait for the product surface's net-worth fan to render at non-zero height."""
    page.add_style_tag(content=deterministic_style())
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-fan-chart='netWorthUsd']").wait_for(state="visible", timeout=30_000)
    page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_label("Metric to plot").wait_for(state="visible", timeout=30_000)
    page.wait_for_function(
        """
        () => {
          const chart = document.querySelector("[data-product-fan-chart='netWorthUsd'] svg[role='img']");
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
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    _wait_for_product_chart_geometry(page)
    _wait_for_terminal_distribution_density(page, min_series=1)


def _select_first_rollout(page: Page) -> None:
    """Select a rollout from the terminal-distribution chart and exercise the marker↔event-table
    cross-selection handshake. A click anywhere in the plot selects the nearest variant line at that
    percentile; clicking at 70% width binds to a mid-upper rollout. Leaves the table-clicked-month
    selected so the screenshot shows event detail."""
    _click_terminal_distribution_percentile(page, percentile=0.7, y_fraction=0.5)
    page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-rollout-event-marker]").first.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)
    page.locator(r"text=/Seed \d+ - (completed|failed m\d+)/").wait_for(state="visible", timeout=30_000)
    marker = page.locator("[data-product-rollout-event-marker]").last
    marker_month = marker.get_attribute("data-product-rollout-event-marker-month")
    assert marker_month is not None
    marker.click()
    page.locator(
        f"[data-product-rollout-event-month='{marker_month}'][data-product-rollout-event-month-selected='true']"
    ).wait_for(state="visible", timeout=30_000)
    marker.click()
    page.locator(
        f"[data-product-rollout-event-month='{marker_month}'][data-product-rollout-event-month-selected='false']"
    ).wait_for(state="visible", timeout=30_000)
    page.locator(
        f"[data-product-rollout-event-marker-month='{marker_month}'][data-product-rollout-event-marker-selected='false']"
    ).first.wait_for(state="visible", timeout=30_000)
    # Pick a table-row month that has a corresponding marker — `monthly_expense` and `outside_rent`
    # have no markers, so the first table row may be a marker-less month.
    first_marker_month = page.locator("[data-product-rollout-event-marker]").first.get_attribute(
        "data-product-rollout-event-marker-month"
    )
    assert first_marker_month is not None
    table_group = page.locator(f"[data-product-rollout-event-month='{first_marker_month}']")
    table_month = first_marker_month
    table_group.click()
    page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='true']"
    ).first.wait_for(state="visible", timeout=30_000)
    table_group.click()
    page.locator(
        f"[data-product-rollout-event-month='{table_month}'][data-product-rollout-event-month-selected='false']"
    ).wait_for(state="visible", timeout=30_000)
    page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='false']"
    ).first.wait_for(state="visible", timeout=30_000)
    table_group.click()
    page.locator(
        f"[data-product-rollout-event-marker-month='{table_month}'][data-product-rollout-event-marker-selected='true']"
    ).first.wait_for(state="visible", timeout=30_000)


def _wait_for_property_panel(page: Page) -> None:
    """Wait for the Base owning rows + the lifecycle timeline editor (prefilled events) to mount."""
    _wait_for_product_page(page)
    # Owning knobs surface as table rows once the (single) Base scenario buys; the lifecycle timeline
    # is now one of those scenario table rows.
    page.locator("[data-product-knob-row='financingKind']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-timeline]").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Timeline (mid-horizon changes)").wait_for(state="visible", timeout=30_000)
    # Three event rows pre-decoded from the URL: set-rented%, capital improvement, sale.
    page.get_by_label("Rented", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_label("Amount", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_label("Closing cost", exact=True).wait_for(state="visible", timeout=30_000)


def _wait_for_distribution_failures(page: Page) -> None:
    """Wait for a scenario whose distribution includes failed rollouts, so the terminal-distribution
    chart's red failure markers render."""
    page.add_style_tag(content=deterministic_style())
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-fan-chart='netWorthUsd']").wait_for(state="visible", timeout=30_000)
    _wait_for_terminal_distribution_density(page, min_series=1)
    page.locator("[data-product-distribution-failed]").first.wait_for(state="visible", timeout=30_000)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    _wait_for_product_chart_geometry(page)


def _wait_for_calibration_page(page: Page) -> None:
    """Wait for the calibration tab's auto-run to land (results, not just the form).

    The tab now auto-runs on load (no button), so the screenshot captures the scored-markets
    table and the issuer mark fan. Hermetic prices are served by the in-process server, so the
    auto-run resolves without touching the network."""
    page.add_style_tag(content=deterministic_style())
    page.locator("[data-augur-surface='calibration']").wait_for(state="visible", timeout=30_000)
    page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=30_000)
    page.locator("[data-augur-tab='calibration'][data-active]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-calibration-catalog]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-calibration-categorical-chart]").first.wait_for(state="visible", timeout=30_000)
    page.locator("[data-calibration-mark-fan]").wait_for(state="visible", timeout=30_000)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")


# A single Base scenario that buys the fixture property `location_a_property` and carries three
# mid-horizon lifecycle events: set rented to 50% at month 24, a $50k capital improvement at month
# 60, and a sale at month 120 with 6% closing cost. The horizon (240 months) rides the tab-shared
# `?h=` control; every other knob inherits `productInputDefaults`. Encoded in the same `?scenarios=`
# base+overrides codec (v2) as the comparison case, just with no variants.
_PROPERTY_LIFECYCLE_SCENARIOS = {
    "v": 2,
    "base": {
        "label": "Base",
        "input": {
            "propertyId": "location_a_property",
            "propertyLifecycleEvents": [
                {"kind": "set_rented_fraction", "month": 24, "rentedFractionPct": 50},
                {"kind": "capital_improvement", "month": 60, "amountUsd": 50000},
                {"kind": "property_sale", "month": 120, "closingCostPct": 6},
            ],
        },
    },
    "variants": [],
}
_PROPERTY_LIFECYCLE_URL = "/product?" + urlencode({"scenarios": json.dumps(_PROPERTY_LIFECYCLE_SCENARIOS), "h": "240"})

# Three-scenario "rent vs. buy A vs. buy B" comparison in the base+overrides codec (v2). The Base
# scenario "Rent" sets only the fields that differ from the product defaults (the codec merges the
# rest over `productInputDefaults`); each variant buys a different fixture property and stops paying
# outside rent. So the editor spreadsheet shows three columns with a per-scenario "Property to buy"
# row (none / Location A / Location B) and an overridden "Monthly rent" row ($3,000 / $0 / $0) — a
# mix of Base, inherited (muted), and overridden (bold + ↩) cells. The whole set rides the
# URL-encoded `?scenarios=` param.
_COMPARISON_SCENARIOS = {
    "v": 2,
    "base": {"label": "Rent", "input": {"monthlyRentUsd": 3000}},
    "variants": [
        {
            "label": "Buy A",
            "overrides": {
                "propertyId": "location_a_property",
                "financingKind": "mortgage",
                "livesHere": True,
                "monthlyRentUsd": 0,
            },
        },
        {
            "label": "Buy B",
            "overrides": {
                "propertyId": "location_b_property",
                "financingKind": "mortgage",
                "livesHere": True,
                "monthlyRentUsd": 0,
            },
        },
    ],
}
_COMPARISON_URL = "/product?" + urlencode({"scenarios": json.dumps(_COMPARISON_SCENARIOS), "h": "240"})

# A single Base scenario engineered to bust a chunk of its rollouts: a high monthly spend against the
# fixture portfolio so weaker-market paths exhaust cash and holdings before the 10y horizon, while
# stronger-market paths survive. This is the only fixture with a non-zero failure rate, so it
# exercises the distribution chart's failed-rollout markers (red dots, pinned at the frozen-to-0
# terminal value). `cashBufferSaleUsd` is raised so funding keeps pace with spend month-to-month —
# a bust then means holdings genuinely ran out, which is market-path-dependent (hence partial).
_FAILURE_SCENARIOS = {
    "v": 2,
    "base": {"label": "Aggressive drawdown", "input": {"monthlySpendUsd": 9000, "cashBufferSaleUsd": 40000}},
    "variants": [],
}
_FAILURE_URL = "/product?" + urlencode({"scenarios": json.dumps(_FAILURE_SCENARIOS), "h": "120"})


def _wait_for_scenario_comparison(page: Page) -> None:
    """Wait for the multi-scenario overlay: the scenario bar, the editor spreadsheet with a Base +
    two variant columns (and the per-scenario "Property to buy" row), three scenario fans + legend,
    and the per-scenario comparison table."""
    page.add_style_tag(content=deterministic_style())
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-scenario-tabs]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-fan-chart='netWorthUsd']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-fan-legend]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-scenario-comparison]").wait_for(state="visible", timeout=30_000)
    # The editor spreadsheet shows Base + the two variants as columns (rows = knobs), including the
    # per-scenario property row.
    page.locator("[data-product-scenario-table]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-knob-row='propertyId']").wait_for(state="visible", timeout=30_000)
    page.wait_for_function('() => document.querySelectorAll("[data-product-scenario-col]").length >= 3', timeout=30_000)
    # All three scenario fans have drawn their median lines.
    page.wait_for_function('() => document.querySelectorAll("[data-product-fan-series]").length >= 3', timeout=30_000)
    # The terminal-distribution chart overlays one dense line per variant (all three present).
    _wait_for_terminal_distribution_density(page, min_series=3)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    _wait_for_product_chart_geometry(page)


def _show_candles(page: Page) -> None:
    """Switch the rollout chart from fans to candles, then select a rollout so the screenshot pins
    the continuous rollout trajectory + event markers over the box-and-whisker candles."""
    page.locator("[data-product-chart-mode-toggle]").get_by_text("Candles", exact=True).click()
    page.locator("[data-product-candle-series]").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_function(
        '() => new Set([...document.querySelectorAll("[data-product-candle-series]")]'
        '.map((node) => node.getAttribute("data-product-candle-series"))).size >= 3',
        timeout=30_000,
    )
    _select_rollout_from_distribution(page)
    page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-rollout-event-marker]").first.wait_for(state="visible", timeout=30_000)


def _select_rollout_from_distribution(page: Page) -> None:
    """Select a rollout directly from the multi-variant terminal-distribution chart. A click binds to
    the nearest variant line at that percentile (making it active), drawing the selection marker on
    the line and the rollout overlay on the timeline chart below, with the events panel carrying the active
    badge."""
    _click_terminal_distribution_percentile(page, percentile=0.6, y_fraction=0.4)
    page.locator("[data-product-distribution-selected]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-selected-rollout-line]").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)


def _focus_active_scenario(page: Page) -> None:
    """Collapse the multi-scenario overlay to just the active scenario via the Compare/Focus toggle,
    then select a rollout. Focus renders the active variant as a full single-scenario fan (the
    pre-comparison view), so the multi-scenario legend disappears; with a rollout selected, the
    events panel below carries the active-scenario badge that names which variant the timeline is."""
    page.locator("[data-product-fan-legend]").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-scenario-focus-toggle]").get_by_text("Focus", exact=True).click()
    page.locator("[data-product-fan-legend]").wait_for(state="detached", timeout=30_000)
    _select_first_rollout(page)


VISUAL_CASES = (
    VisualCase(
        name="product_cash_runway", path="/product", wait_ready=_wait_for_product_page, interact=_select_first_rollout
    ),
    VisualCase(name="product_property_lifecycle", path=_PROPERTY_LIFECYCLE_URL, wait_ready=_wait_for_property_panel),
    VisualCase(name="product_scenario_comparison", path=_COMPARISON_URL, wait_ready=_wait_for_scenario_comparison),
    VisualCase(
        name="product_distribution_multi",
        path=_COMPARISON_URL,
        wait_ready=_wait_for_scenario_comparison,
        interact=_select_rollout_from_distribution,
    ),
    VisualCase(
        name="product_scenario_candles",
        path=_COMPARISON_URL,
        wait_ready=_wait_for_scenario_comparison,
        interact=_show_candles,
    ),
    VisualCase(
        name="product_scenario_focus",
        path=_COMPARISON_URL,
        wait_ready=_wait_for_scenario_comparison,
        interact=_focus_active_scenario,
    ),
    VisualCase(name="product_distribution_failures", path=_FAILURE_URL, wait_ready=_wait_for_distribution_failures),
    VisualCase(name="calibration_page", path="/product?tab=calibration", wait_ready=_wait_for_calibration_page),
)


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    browser = launch_deterministic_browser(playwright_sync)
    try:
        yield browser
    finally:
        browser.close()


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
    port = pick_free_port("127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="augur-visual-uvicorn", daemon=True)
    thread.start()
    try:
        wait_for_port("127.0.0.1", port, timeout_secs=30)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = deterministic_browser_context(browser, viewport=SCREENSHOT_VIEWPORT, frozen_now_ms=FROZEN_NOW_MS)
    page = context.new_page()
    page.on(
        "pageerror",
        lambda err: page.evaluate(
            "err => { window.__jsErrors = window.__jsErrors || []; window.__jsErrors.push(String(err)); }", str(err)
        ),
    )
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


@pytest.fixture
def page_errors(page: Page) -> list[str]:
    """Uncaught JS exceptions thrown in the page during a test.

    esbuild bundles undefined-variable and bad-prop bugs without complaint, and a screenshot diff
    won't always surface a render-time `ReferenceError` (the crashing subtree may be off the tested
    path). Collecting `pageerror` events lets the visual test fail loudly on any uncaught exception."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return errors


def _take_stable_full_page_screenshot(page: Page, target_path: Path) -> Path:
    # Pin the `sticky top-0` header to the top of the full-page capture: Playwright paints a
    # sticky element at its last on-screen position, so a mid-page scroll (e.g. after a rollout
    # interaction) would otherwise leave the header floating over the middle of the screenshot.
    page.evaluate("() => window.scrollTo(0, 0)")
    previous_bytes: bytes | None = None
    previous_path: Path | None = None
    for attempt in range(6):
        attempt_path = target_path.with_name(f"{target_path.stem}.attempt{attempt}{target_path.suffix}")
        page.screenshot(path=str(attempt_path), full_page=True, animations="disabled", caret="hide", scale="css")
        current_bytes = attempt_path.read_bytes()
        if current_bytes == previous_bytes:
            shutil.copy(attempt_path, target_path)
            return target_path
        previous_bytes = current_bytes
        previous_path = attempt_path
        page.wait_for_timeout(150)
    assert previous_path is not None
    shutil.copy(previous_path, target_path)
    return target_path


def _render_case(page: Page, origin: str, case: VisualCase, out_dir: Path, suffix: str) -> Path:
    page.goto(f"{origin}{case.path}", wait_until="networkidle", timeout=60_000)
    try:
        case.wait_ready(page)
    except Exception:
        debug_dir = undeclared_outputs_dir()
        page.screenshot(path=str(debug_dir / f"{case.name}.{suffix}.debug.png"), full_page=True)
        dom = page.content()
        (debug_dir / f"{case.name}.{suffix}.debug.html").write_text(dom[:5000])
        errors = page.evaluate("() => window.__jsErrors?.join('\\n') ?? 'no __jsErrors'")
        (debug_dir / f"{case.name}.{suffix}.debug.txt").write_text(f"JS errors: {errors}\nURL: {page.url}")
        raise
    page.goto(page.url, wait_until="networkidle", timeout=60_000)
    case.wait_ready(page)
    if case.interact is not None:
        case.interact(page)
        _wait_for_product_chart_geometry(page)
    actual_path = out_dir / f"{case.name}.{suffix}.png"
    return _take_stable_full_page_screenshot(page, actual_path)


@pytest.mark.parametrize("case", VISUAL_CASES, ids=[case.name for case in VISUAL_CASES])
def test_augur_visual_golden(
    page: Page, page_errors: list[str], augur_server: str, tmp_path: Path, case: VisualCase
) -> None:
    undeclared_dir = undeclared_outputs_dir()
    first_path = _render_case(page, augur_server, case, tmp_path, "first")
    second_path = _render_case(page, augur_server, case, tmp_path, "second")
    if page_errors:
        raise AssertionError(f"{case.name}: uncaught page error(s) during render:\n" + "\n".join(page_errors))
    if first_path.read_bytes() != second_path.read_bytes():
        shutil.copy(first_path, undeclared_dir / f"{case.name}.first.png")
        shutil.copy(second_path, undeclared_dir / f"{case.name}.second.png")
        raise AssertionError(
            f"{case.name} visual render is not deterministic across reloads; "
            f"inspect {case.name}.first.png and {case.name}.second.png in {undeclared_dir}"
        )

    # Always retain the candidate render + visual-review manifest for the PR
    # visual-review publisher (devinfra/pr_visuals/publisher.py).
    out_name = f"{case.name}.png"
    retain_review_asset(first_path, title="Augur pages", label=case.name.replace("_", " "), name=out_name)

    if os.environ.get("UPDATE_GOLDEN") == "1":
        return

    try:
        expected_path = get_required_path(f"_main/finance/augur/frontend/__screenshots__/{out_name}")
    except RuntimeError:
        raise AssertionError(
            f"No Augur visual golden checked in for {out_name}. Re-run with UPDATE_GOLDEN=1 "
            f"and copy the produced PNG from undeclared outputs into augur/frontend/__screenshots__/."
        ) from None

    assert_png_matches_golden(
        first_path, expected_path, name=case.name, out_dir=undeclared_dir, tolerance=0.0, intensity_threshold=1
    )


if __name__ == "__main__":
    pytest_bazel.main()
