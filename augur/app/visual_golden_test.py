"""Full-page visual goldens for representative public Augur URLs.

Update flow for intentional frontend changes:

    nix develop --command bazelisk test //augur/app:visual_golden_test \
        --test_env=UPDATE_GOLDEN=1 \
        --remote_upload_local_results=false --nocache_test_results

Then copy the produced PNGs from the test's undeclared outputs into
`augur/app/__screenshots__/` and rerun this test without `UPDATE_GOLDEN`.
With BuildBuddy/RBE, use the invocation id printed by Bazel:

    bbapi artifact "$INV" test.outputs/distribution_default.png \
        > augur/app/__screenshots__/distribution_default.png
    bbapi artifact "$INV" test.outputs/distribution_fan.png \
        > augur/app/__screenshots__/distribution_fan.png
    bbapi artifact "$INV" test.outputs/trajectory_scenario_2_rollout_3.png \
        > augur/app/__screenshots__/trajectory_scenario_2_rollout_3.png
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.png_diff import assert_png_matches_golden
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright, ViewportSize


@dataclass(frozen=True)
class VisualCase:
    name: str
    path: str
    visible_text: str
    hidden_text: str
    min_fan_band_height: float | None = None


def _encode_visual_state(scenario_set_input: dict[str, object]) -> str:
    payload = {"version": 4, "scenario_set_input": scenario_set_input}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload_json).decode().rstrip("=")


def _visual_path(path: str, scenario_set_input: dict[str, object], *, scenario: str, rollout: int = 0) -> str:
    query = urllib.parse.urlencode(
        {"state": _encode_visual_state(scenario_set_input), "scenario": scenario, "rollout": str(rollout)}
    )
    return f"{path}?{query}"


FAN_STATE: dict[str, object] = {
    "title": "Augur distribution fan fixture",
    "market_request": {"market_model_id": "visual_fan", "rollout_count": 32, "horizon_months": 36, "seed": 23},
    "report_spec": {"percentiles": [5, 25, 50, 75, 95], "include_monthly_columns": True},
    "scenarios": [
        {
            "identity": {
                "scenario_id": "location_a_fan",
                "label": "Location A 3-year fan",
                "enabled": True,
                "color": "#2563eb",
            },
            "property_and_location": {"property_id": "location_a_property"},
            "actors_and_ownership": {"actor_policy": "owner_only", "partner_payment_monthly_usd": 2000},
            "timeline": {"hold_years": 3},
            "financing": {"financing_mode": "fixed_30", "down_payment_pct": 25, "credit_score": 776},
            "initial_balance_sheet": {
                "initial_checking_usd": 75000,
                "starting_portfolio_usd": 350000,
                "private_equity_units": 2500,
            },
            "policies": {
                "liquid_reserve_policy": "checking_floor_sp500",
                "checking_floor_usd": 100000,
                "checking_sale_amount_usd": 50000,
            },
        },
        {
            "identity": {
                "scenario_id": "location_b_fan",
                "label": "Location B shared 3-year fan",
                "enabled": True,
                "color": "#dc2626",
            },
            "property_and_location": {"property_id": "location_b_property"},
            "actors_and_ownership": {"actor_policy": "owner_plus_partner", "partner_payment_monthly_usd": 2750},
            "timeline": {"hold_years": 3},
            "financing": {"financing_mode": "fixed_30", "down_payment_pct": 20, "credit_score": 776},
            "occupancy_and_rental": {
                "owner_residence_mode": "selected_property",
                "rental_use_policy": "rent_rooms_while_owner_lives_there",
                "rooms_rented_while_living": 1,
                "room_rent_monthly_usd": 1400,
                "room_vacancy_pct": 8,
                "vacancy_pct": 8,
                "management_fee_pct": 8,
                "leasing_fee_pct": 0,
            },
            "initial_balance_sheet": {
                "initial_checking_usd": 75000,
                "starting_portfolio_usd": 350000,
                "private_equity_units": 2500,
            },
            "policies": {
                "liquid_reserve_policy": "checking_floor_sp500",
                "checking_floor_usd": 100000,
                "checking_sale_amount_usd": 50000,
                "private_equity_sale_policy": "liquid_net_worth_floor",
                "private_equity_liquid_net_worth_floor_usd": 150000,
                "private_equity_tender_sale_amount_usd": 50000,
            },
        },
    ],
}


VISUAL_CASES = (
    VisualCase(
        name="distribution_default",
        path="/distribution?scenario=scenario_1&rollout=0",
        visible_text="Terminal scenario comparison",
        hidden_text="Selected path monthly ledger",
    ),
    VisualCase(
        name="distribution_fan",
        path=_visual_path("/distribution", FAN_STATE, scenario="location_a_fan"),
        visible_text="Scenario probability fans",
        hidden_text="Selected path monthly ledger",
        min_fan_band_height=80,
    ),
    VisualCase(
        name="trajectory_scenario_2_rollout_3",
        path="/trajectory?scenario=scenario_2&rollout=3",
        visible_text="Selected path monthly ledger",
        hidden_text="Terminal scenario comparison",
    ),
)

SCREENSHOT_VIEWPORT: ViewportSize = {"width": 1280, "height": 1000}
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.

DETERMINISTIC_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--font-render-hinting=none",
    "--disable-font-subpixel-positioning",
    "--disable-lcd-text",
    "--force-color-profile=srgb",
    "--disable-accelerated-2d-canvas",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    "--disable-skia-runtime-opts",
    "--disable-partial-raster",
    "--disable-backing-store-limit",
    "--use-gl=swiftshader",
    "--force-device-scale-factor=1",
    "--disable-features=CalculateNativeWinOcclusion,VizDisplayCompositor",
    "--disable-accelerated-video-decode",
    "--disable-canvas-aa",
    "--disable-2d-canvas-clip-aa",
    "--disable-webgl",
    "--disable-webgl2",
    "--blink-settings=imageAnimationPolicy=noAnimation",
    "--disable-smooth-scrolling",
    "--disable-threaded-animation",
    "--disable-threaded-scrolling",
    "--disable-checker-imaging",
]


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    browser = playwright_sync.chromium.launch(
        headless=True, executable_path=executable, args=DETERMINISTIC_BROWSER_ARGS
    )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture(scope="module")
def augur_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("augur-visual-server")
    out = undeclared_outputs_dir()
    server_log = (out / "augur-visual-server.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/augur/app/server")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/augur/app/testdata/config.yaml")),
            "--provider",
            "simple",
            "--rollout-samples",
            "8",
            "--max-rollout-samples",
            "32",
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


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(
        viewport=SCREENSHOT_VIEWPORT,
        device_scale_factor=1,
        color_scheme="light",
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="UTC",
    )
    frozen_clock_script = """
        ((nowMs) => {
          const OriginalDate = Date;
          class FrozenDate extends OriginalDate {
            constructor(...args) {
              if (args.length === 0) {
                super(nowMs);
              } else {
                super(...args);
              }
            }
            static now() {
              return nowMs;
            }
          }
          globalThis.Date = FrozenDate;
        })(__FROZEN_NOW_MS__);
        """
    context.add_init_script(frozen_clock_script.replace("__FROZEN_NOW_MS__", str(FROZEN_NOW_MS)))
    page = context.new_page()
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


def _deterministic_style() -> str:
    font_bytes = get_required_path("_main/util/testing/frontend_visual/fonts/Inter.woff2").read_bytes()
    font_base64 = base64.b64encode(font_bytes).decode()
    return f"""
    @font-face {{
      font-family: "Inter";
      src: url("data:font/woff2;base64,{font_base64}") format("woff2");
      font-weight: 100 900;
      font-display: block;
    }}
    :root,
    body,
    * {{
      caret-color: transparent !important;
      font-family: "Inter", sans-serif !important;
      -webkit-font-smoothing: none !important;
      -moz-osx-font-smoothing: unset !important;
      font-smooth: never !important;
      text-rendering: geometricPrecision !important;
    }}
    *,
    *::before,
    *::after {{
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      scroll-behavior: auto !important;
    }}
    """


def _wait_for_augur_page(page: Page, case: VisualCase) -> None:
    page.add_style_tag(content=_deterministic_style())
    page.get_by_role("heading", name="Augur", exact=True).wait_for(state="visible", timeout=30_000)
    page.get_by_text(case.visible_text).wait_for(state="visible", timeout=30_000)
    page.wait_for_function(
        "() => new URL(window.location.href).searchParams.has('state') "
        "&& !document.body.innerText.includes('Running...')"
    )
    if case.min_fan_band_height is not None:
        page.wait_for_function(
            """
            (minFanBandHeight) => {
              const chart = Array.from(document.querySelectorAll('svg[role="img"]')).find((svg) =>
                (svg.getAttribute("aria-label") || "").includes("probability fan chart")
              );
              if (!chart) return false;
              const heights = Array.from(chart.querySelectorAll("polygon")).map((polygon) => {
                const points = (polygon.getAttribute("points") || "")
                  .trim()
                  .split(/\\s+/)
                  .map((point) => Number(point.split(",")[1]))
                  .filter(Number.isFinite);
                if (points.length === 0) return 0;
                return Math.max(...points) - Math.min(...points);
              });
              return Math.max(0, ...heights) >= minFanBandHeight;
            }
            """,
            arg=case.min_fan_band_height,
            timeout=30_000,
        )
    assert page.get_by_text(case.hidden_text).count() == 0
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.evaluate("() => document.fonts.ready.then(() => true)")


def _render_case(page: Page, origin: str, case: VisualCase, out_dir: Path, suffix: str) -> Path:
    page.goto(f"{origin}{case.path}", wait_until="networkidle", timeout=60_000)
    _wait_for_augur_page(page, case)
    stable_url = page.url
    page.goto(stable_url, wait_until="networkidle", timeout=60_000)
    _wait_for_augur_page(page, case)
    actual_path = out_dir / f"{case.name}.{suffix}.png"
    page.screenshot(path=str(actual_path), full_page=True, animations="disabled", caret="hide", scale="css")
    return actual_path


@pytest.mark.parametrize("case", VISUAL_CASES, ids=[case.name for case in VISUAL_CASES])
def test_augur_full_page_visual_golden(page: Page, augur_server: str, tmp_path: Path, case: VisualCase) -> None:
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
        expected_path = get_required_path(f"_main/augur/app/__screenshots__/{out_name}")
    except RuntimeError:
        shutil.copy(first_path, undeclared_dir / out_name)
        raise AssertionError(
            f"No Augur visual golden checked in for {out_name}. Re-run with UPDATE_GOLDEN=1 "
            f"and copy the produced PNG from undeclared outputs into augur/app/__screenshots__/."
        ) from None

    assert_png_matches_golden(
        first_path, expected_path, name=case.name, out_dir=undeclared_dir, tolerance=0.0, intensity_threshold=1
    )


if __name__ == "__main__":
    pytest_bazel.main()
