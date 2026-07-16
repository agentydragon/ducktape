"""Full-page visual goldens for each casino view + each game.

Run produces a PNG per (view, viewport) case and compares against the checked-in
baseline under `x/study_casino/frontend/__screenshots__/`.

Every run also retains the rendered PNGs plus a `visual-review.json` manifest in
undeclared outputs, so trusted CI (`devinfra/ci/pr_visuals.py` via the
"Publish PR visuals" workflow) publishes the views as a browsable bundle and
comments the link on the PR.

Update flow for intentional frontend changes:

    bbr test --test_env=UPDATE_GOLDEN=1 --nocache_test_results \\
      //x/study_casino/tests:visual_golden_test

Then download the produced PNGs from the test's undeclared outputs and copy
them into `x/study_casino/frontend/__screenshots__/`. Using the
invocation id printed by bbr:

    INV="<invocation-id>"
    for f in $(bbapi artifact list "$INV" | awk '/\\.png$/ {print $NF}'); do
      bbapi artifact download "$INV" "test.outputs/$f" \\
        -o x/study_casino/frontend/__screenshots__/"$f"
    done
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
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
import uvicorn
from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.oci import load_oci_image
from util.testing.png_diff import assert_png_matches_golden
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_review import retain_review_asset
from x.study_casino.app import create_app
from x.study_casino.config import Settings

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright, ViewportSize


# Two viewports per case: a desktop width that exercises the two-column casino
# layouts and a mobile width that should show the single-column responsive
# variant. The mobile width matches iPhone 14 logical CSS pixels (most narrow
# phones land between 360 and 420 CSS px).
DESKTOP_VIEWPORT: ViewportSize = {"width": 1280, "height": 900}
MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Frozen wall-clock so the active-session timer, today's totals, and any
# Date.now()-driven UI render identically across runs.
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.


@dataclass(frozen=True)
class Case:
    name: str
    query: str  # appended to "/", e.g. "?view=casino&game=roulette"
    visible_text: str  # selector text that must be visible before screenshotting
    viewport: ViewportSize


def _both_widths(slug: str, query: str, visible_text: str) -> tuple[Case, Case]:
    return (
        Case(name=f"{slug}.desktop", query=query, visible_text=visible_text, viewport=DESKTOP_VIEWPORT),
        Case(name=f"{slug}.mobile", query=query, visible_text=visible_text, viewport=MOBILE_VIEWPORT),
    )


CASES: tuple[Case, ...] = (
    *_both_widths("study", "?view=study", "Today"),
    *_both_widths("casino_roulette", "?view=casino&game=roulette", "ROULETTE"),
    *_both_widths("casino_blackjack", "?view=casino&game=blackjack", "BLACKJACK"),
    *_both_widths("casino_slots", "?view=casino&game=slots", "SLOTS"),
    *_both_widths("prizes", "?view=prizes", "The Vault"),
    *_both_widths("stats", "?view=stats", "The Ledger"),
)


# Subset of Augur's deterministic browser flags — disables animation, sub-pixel
# font rendering, GPU compositing, and webgl, so the rendered PNGs are stable
# across hosts.
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
    instance = playwright_sync.chromium.launch(
        headless=True, executable_path=executable, args=DETERMINISTIC_BROWSER_ARGS
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def casino_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """uvicorn-backed casino server with a fresh Postgres testcontainer."""
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="casino")
    container.start()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        db_url = f"postgresql+psycopg://postgres:postgres@{host}:{port}/casino"

        tmp_path = tmp_path_factory.mktemp("casino-visual-server")
        frontend_dist = get_required_path("_main/x/study_casino/frontend/dist/index.html").parent

        # The visual goldens want a non-empty UI for prizes/stats. Seed a few
        # sessions and a token balance via the server's own action endpoints
        # right after startup, before the screenshots run.
        settings = Settings(database_url=db_url, frontend_dist_dir=frontend_dist, admin_users={"default"})
        app = create_app(settings)
        server_port = pick_free_port("127.0.0.1")
        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=server_port, log_level="warning"))

        thread = threading.Thread(target=_run_uvicorn, args=(server,), name="casino-visual-uvicorn", daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("casino server did not start within 30s")

        origin = f"http://127.0.0.1:{server_port}"
        _seed_fixture_state(origin)

        try:
            yield origin
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        # Drop the temp dir
        shutil.rmtree(tmp_path, ignore_errors=True)
    finally:
        container.stop()


def _run_uvicorn(server: uvicorn.Server) -> None:
    asyncio.run(server.serve())


def _seed_fixture_state(origin: str) -> None:
    """Populate a small fixture state so visual goldens have content to render.

    Adds two completed past sessions (so Stats / Study panels show data) and
    converts a chunk to tokens (so the Vault shows redeemable balance + the
    casino games have credits to wager).
    """
    _post(
        origin,
        "/actions/session/add-past",
        {
            "client_action_id": "visual-seed-session-1",
            "subject": "Biochem",
            "seconds": 90 * 60,
            "ended_at_ms": FROZEN_NOW_MS - 6 * 3600 * 1000,
        },
    )
    _post(
        origin,
        "/actions/session/add-past",
        {
            "client_action_id": "visual-seed-session-2",
            "subject": "Pharmacology",
            "seconds": 45 * 60,
            "ended_at_ms": FROZEN_NOW_MS - 2 * 3600 * 1000,
        },
    )
    _post(origin, "/actions/convert", {"client_action_id": "visual-seed-convert", "amount": 50})


def _post(origin: str, path: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"seed {path} failed: HTTP {response.status}")


def _deterministic_style() -> str:
    return """
    body, * {
      caret-color: transparent !important;
      -webkit-font-smoothing: none !important;
      -moz-osx-font-smoothing: unset !important;
      font-smooth: never !important;
      text-rendering: geometricPrecision !important;
    }
    *, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      scroll-behavior: auto !important;
    }
    """


def _frozen_clock_init() -> str:
    return f"""
    ((nowMs) => {{
      const OriginalDate = Date;
      class FrozenDate extends OriginalDate {{
        constructor(...args) {{
          if (args.length === 0) {{
            super(nowMs);
          }} else {{
            super(...args);
          }}
        }}
        static now() {{
          return nowMs;
        }}
      }}
      globalThis.Date = FrozenDate;
    }})({FROZEN_NOW_MS});
    """


def _render_case(browser: Browser, origin: str, case: Case, out_dir: Path, suffix: str) -> Path:
    context = browser.new_context(
        viewport=case.viewport,
        device_scale_factor=1,
        color_scheme="dark",
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="UTC",
    )
    context.add_init_script(_frozen_clock_init())
    page = context.new_page()
    try:
        page.goto(f"{origin}/{case.query}", wait_until="networkidle", timeout=30_000)
        page.add_style_tag(content=_deterministic_style())
        page.get_by_text(case.visible_text).first.wait_for(state="visible", timeout=15_000)
        # Force fonts to settle before screenshot.
        page.evaluate("() => document.fonts.ready.then(() => true)")
        actual_path = out_dir / f"{case.name}.{suffix}.png"
        page.screenshot(path=str(actual_path), full_page=True, animations="disabled", caret="hide", scale="css")
        return actual_path
    finally:
        page.close()
        context.close()


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_casino_visual_golden(browser: Browser, casino_server: str, tmp_path: Path, case: Case) -> None:
    undeclared_dir = undeclared_outputs_dir()
    first_path = _render_case(browser, casino_server, case, tmp_path, "first")
    second_path = _render_case(browser, casino_server, case, tmp_path, "second")
    if first_path.read_bytes() != second_path.read_bytes():
        shutil.copy(first_path, undeclared_dir / f"{case.name}.first.png")
        shutil.copy(second_path, undeclared_dir / f"{case.name}.second.png")
        raise AssertionError(
            f"{case.name} visual render is not deterministic across reloads; "
            f"inspect {case.name}.first.png and {case.name}.second.png in {undeclared_dir}"
        )

    # Always retain the candidate render + visual-review manifest for the PR
    # visual-review publisher (devinfra/ci/pr_visuals.py).
    out_name = f"{case.name}.png"
    retain_review_asset(
        first_path, title="Study Casino views", label=case.name.replace("_", " ").replace(".", " · "), name=out_name
    )

    if os.environ.get("UPDATE_GOLDEN") == "1":
        return

    try:
        expected_path = get_required_path(f"_main/x/study_casino/frontend/__screenshots__/{out_name}")
    except RuntimeError:
        raise AssertionError(
            f"No casino visual golden checked in for {out_name}. Re-run with UPDATE_GOLDEN=1 "
            f"and copy the produced PNG from undeclared outputs into "
            f"x/study_casino/frontend/__screenshots__/."
        ) from None

    assert_png_matches_golden(
        first_path, expected_path, name=case.name, out_dir=undeclared_dir, tolerance=0.0, intensity_threshold=2
    )


if __name__ == "__main__":
    pytest_bazel.main()
