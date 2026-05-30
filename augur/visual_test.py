"""Full-page visual goldens for representative public Augur URLs.

Update flow for intentional frontend changes:

    nix develop --command bazelisk test //augur:visual_test \\
        --test_env=UPDATE_GOLDEN=1 \\
        --remote_upload_local_results=false --nocache_test_results

Then copy the produced PNGs from the test's undeclared outputs into
`augur/frontend/__screenshots__/` and rerun this test without `UPDATE_GOLDEN`.
With BuildBuddy/RBE, use the invocation id printed by Bazel:

    bbapi artifact download "$INV" "test.outputs/product_cash_runway.png" \\
        -o augur/frontend/__screenshots__/product_cash_runway.png
"""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_bazel
import uvicorn

from augur.api.config import load_augur_config
from augur.calibration.catalog import MarketCatalog
from augur.calibration.testing import mock_manifold_client
from augur.dev_server import build_dev_app
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port, wait_for_port
from util.testing.frontend_visual import (
    deterministic_browser_context,
    deterministic_style,
    launch_deterministic_browser,
)
from util.testing.png_diff import assert_png_matches_golden
from util.testing.undeclared_outputs import undeclared_outputs_dir

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
          const horizonInput = document.querySelector("input[aria-label='Horizon']");
          if (!chart || !horizonInput) return false;
          const horizonMonths = Number(String(horizonInput.value || "").replace(/,/g, ""));
          if (!Number.isFinite(horizonMonths)) return false;
          const expectedFinalYear = `${Math.max(1, Math.ceil(horizonMonths / 12))} yr`;
          const finalYearTick = Array.from(chart.querySelectorAll("text")).find(
            (node) => node.textContent.trim() === expectedFinalYear
          );
          if (!finalYearTick) return false;
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
    assert page.get_by_text("Terminal scenario comparison").count() == 0
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    _wait_for_product_chart_geometry(page)


def _select_first_rollout(page: Page) -> None:
    """Click the first rollout sliver and exercise the marker↔event-table cross-selection
    handshake. Leaves the table-clicked-month selected so the screenshot shows event detail."""
    page.locator("[data-product-rollout-sliver]").first.click()
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
    """Wait for the property panel + lifecycle editor to mount with the prefilled events."""
    _wait_for_product_page(page)
    page.locator("[data-product-property-panel]").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Timeline (mid-horizon changes)").wait_for(state="visible", timeout=30_000)
    # Three event rows pre-decoded from the URL: set-rented%, capital improvement, sale.
    page.get_by_label("Rented", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_label("Amount", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_label("Closing cost", exact=True).wait_for(state="visible", timeout=30_000)


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
    page.locator("[data-calibration-mark-fan]").wait_for(state="visible", timeout=30_000)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")


# `?s=4.240...........location_a_property` selects the fixture property `location_a_property`
# with horizonMonths=240. The dots between "240" and the value are the empty positions for
# firstSeed..rentalLocationId (all defaults). `?lc=` carries three lifecycle events.
# Schema version 4 (matches the v4 schema bump that dropped `rolloutCount`, now the tab-shared
# `?n=` control).
_PROPERTY_LIFECYCLE_URL = "/product?s=4.240...........location_a_property&lc=r24:50~c60:50000~s120:6"

VISUAL_CASES = (
    VisualCase(
        name="product_cash_runway", path="/product", wait_ready=_wait_for_product_page, interact=_select_first_rollout
    ),
    VisualCase(name="product_property_lifecycle", path=_PROPERTY_LIFECYCLE_URL, wait_ready=_wait_for_property_panel),
    VisualCase(name="calibration_page", path="/product?tab=calibration", wait_ready=_wait_for_calibration_page),
)


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    browser = launch_deterministic_browser(playwright_sync)
    try:
        yield browser
    finally:
        browser.close()


def _hermetic_prices() -> dict[str, float]:
    """A fixed live price for every market in the example catalog.

    The calibration tab auto-runs on load and scores every catalog market, so the in-process
    server needs a probability for each `manifold_id` to resolve the run with no network. The
    exact values only need to be plausible and deterministic; a gentle spread keeps the scored
    table and surfaced list visually populated."""
    catalog = MarketCatalog.from_yaml(get_required_path("_main/augur/calibration/example_openai_catalog.yaml"))
    return {market.manifold_id: 0.3 + 0.4 * (index % 3) / 2 for index, market in enumerate(catalog.markets)}


@pytest.fixture(scope="module")
def augur_server() -> Iterator[str]:
    config = load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))
    # Inject a hermetic Manifold client so the calibration tab's auto-run never hits the network.
    app = build_dev_app(config, price_client=mock_manifold_client(_hermetic_prices()))
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
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


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
    case.wait_ready(page)
    page.goto(page.url, wait_until="networkidle", timeout=60_000)
    case.wait_ready(page)
    if case.interact is not None:
        case.interact(page)
        _wait_for_product_chart_geometry(page)
    actual_path = out_dir / f"{case.name}.{suffix}.png"
    return _take_stable_full_page_screenshot(page, actual_path)


@pytest.mark.parametrize("case", VISUAL_CASES, ids=[case.name for case in VISUAL_CASES])
def test_augur_visual_golden(page: Page, augur_server: str, tmp_path: Path, case: VisualCase) -> None:
    undeclared_dir = undeclared_outputs_dir()
    first_path = _render_case(page, augur_server, case, tmp_path, "first")
    second_path = _render_case(page, augur_server, case, tmp_path, "second")
    if first_path.read_bytes() != second_path.read_bytes():
        shutil.copy(first_path, undeclared_dir / f"{case.name}.first.png")
        shutil.copy(second_path, undeclared_dir / f"{case.name}.second.png")
        raise AssertionError(
            f"{case.name} visual render is not deterministic across reloads; "
            f"inspect {case.name}.first.png and {case.name}.second.png in {undeclared_dir}"
        )

    out_name = f"{case.name}.png"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        shutil.copy(first_path, undeclared_dir / out_name)
        return

    try:
        expected_path = get_required_path(f"_main/augur/frontend/__screenshots__/{out_name}")
    except RuntimeError:
        shutil.copy(first_path, undeclared_dir / out_name)
        raise AssertionError(
            f"No Augur visual golden checked in for {out_name}. Re-run with UPDATE_GOLDEN=1 "
            f"and copy the produced PNG from undeclared outputs into augur/frontend/__screenshots__/."
        ) from None

    assert_png_matches_golden(
        first_path, expected_path, name=case.name, out_dir=undeclared_dir, tolerance=0.0, intensity_threshold=1
    )


if __name__ == "__main__":
    pytest_bazel.main()
