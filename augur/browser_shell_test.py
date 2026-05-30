"""Public Augur product-surface browser smoke test against the Bazel-runnable server."""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright

_PROPERTY_LIFECYCLE_URL = "/product?s=4.240...........location_a_property&lc=r24:50~c60:50000~s120:6"


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    browser = playwright_sync.chromium.launch(
        headless=True,
        executable_path=executable,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


@pytest.fixture
def augur_server(tmp_path: Path) -> Iterator[str]:
    out = undeclared_outputs_dir()
    server_log = (out / "augur-server.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/augur/dev")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/augur/api/testdata/config.yaml")),
        ],
        env={
            **os.environ,
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
            except (OSError, urllib.error.URLError):
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


def test_product_shell_renders_metric_fan_charts(page: Page, augur_server: str) -> None:
    """Smoke-test the product surface end-to-end: load `/product`, select a few metrics,
    confirm the matching fan chart renders for each."""
    page.goto(f"{augur_server}/product", wait_until="domcontentloaded")
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    page.get_by_label("Metric to plot").wait_for(state="visible", timeout=15_000)
    page.get_by_label("Metric to plot").select_option("cash_usd")
    page.locator("[data-product-fan-chart='cashUsd']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-chart-scale-control]").get_by_text("Log", exact=True).click()
    page.locator("[data-product-fan-chart='cashUsd'][data-product-scale='log']").wait_for(
        state="visible", timeout=15_000
    )
    page.locator("[data-product-histogram-scale='log']").wait_for(state="visible", timeout=15_000)
    page.locator("[data-product-chart-scale-control]").get_by_text("Linear", exact=True).click()
    page.locator("[data-product-fan-chart='cashUsd'][data-product-scale='linear']").wait_for(
        state="visible", timeout=15_000
    )
    page.locator("[data-product-histogram-scale='linear']").wait_for(state="visible", timeout=15_000)
    page.get_by_label("Metric to plot").select_option("holding_value_usd")
    page.locator("[data-product-fan-chart='holdingValueUsd']").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Initial portfolio").wait_for(state="visible", timeout=15_000)
    # Grand total (cash + holdings) is shown in the collapsed accordion summary.
    assert page.locator("[data-product-portfolio-subtotal='total']").inner_text() == "$1,110,500"
    # Open the accordion to see per-bucket subtotals inline with their positions.
    page.get_by_text("Initial portfolio").click()
    assert page.locator("[data-product-portfolio-subtotal='public-securities']").inner_text() == "$835,500"
    assert page.locator("[data-product-portfolio-subtotal='private-securities']").inner_text() == "$25,000"


def test_property_recurring_expense_events_start_hidden_on_rollout_graph(page: Page, augur_server: str) -> None:
    page.goto(f"{augur_server}{_PROPERTY_LIFECYCLE_URL}", wait_until="domcontentloaded")
    page.locator("[data-augur-surface='product']").wait_for(state="visible", timeout=15_000)
    page.locator("[data-product-fan-chart='netWorthUsd']").wait_for(state="visible", timeout=30_000)
    page.locator("[data-product-rollout-sliver]").first.click()
    page.get_by_text("Selected rollout events").wait_for(state="visible", timeout=30_000)
    page.get_by_text("Event kinds").wait_for(state="visible", timeout=30_000)
    legend = page.get_by_label("Event-kind visibility legend")

    for label, event_kind in [
        ("Property tax", "property_tax_payment"),
        ("Homeowners insurance", "homeowners_insurance_payment"),
        ("Maintenance", "property_maintenance_payment"),
    ]:
        button = legend.get_by_role("button", name=re.compile(label))
        button.wait_for(state="visible", timeout=15_000)
        assert button.get_attribute("aria-pressed") == "false"
        assert page.locator(f"[data-product-rollout-event-marker='{event_kind}']").count() == 0


if __name__ == "__main__":
    pytest_bazel.main()
