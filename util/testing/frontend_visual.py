"""Shared helpers for Python Playwright visual render-health tests.

The Chromium flag set and the frozen-clock init script are single-sourced with
the JS Puppeteer launcher (`frontend_visual/launcher.mjs`): both read
`util/testing/chromium-flags.json` and `util/testing/frozen-clock.js` (kept at
this level — a data file under `frontend_visual/` would shadow this module as
a namespace package), and both resolve the hermetic browser from
`CHROMIUM_HEADLESS_SHELL`.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from util.bazel.runfiles import get_required_path

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright, ViewportSize


_FLAGS = json.loads(get_required_path("_main/util/testing/chromium-flags.json").read_text())
# Makes headless Chromium run in containerized/RBE environments.
CONTAINER_BASE_BROWSER_ARGS: list[str] = _FLAGS["containerBase"]
# Container base plus font/raster/compositing/animation pinning for stable renders.
DETERMINISTIC_BROWSER_ARGS: list[str] = CONTAINER_BASE_BROWSER_ARGS + _FLAGS["deterministicExtra"]


def chromium_executable() -> str | None:
    """The hermetic headless-shell path from `CHROMIUM_HEADLESS_SHELL`, or None
    to fall back to Playwright's own browser resolution (local runs)."""
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    return str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None


def launch_deterministic_browser(playwright_sync: Playwright) -> Browser:
    return playwright_sync.chromium.launch(
        headless=True, executable_path=chromium_executable(), args=DETERMINISTIC_BROWSER_ARGS
    )


def frozen_clock_script(now_ms: int) -> str:
    source = get_required_path("_main/util/testing/frozen-clock.js").read_text()
    return f"(() => {{ {source} frozenClock({now_ms}); }})();"


def deterministic_browser_context(
    browser: Browser,
    *,
    viewport: ViewportSize,
    frozen_now_ms: int,
    color_scheme: Literal["dark", "light", "no-preference", "null"] = "light",
) -> BrowserContext:
    context = browser.new_context(
        viewport=viewport,
        device_scale_factor=1,
        color_scheme=color_scheme,
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="UTC",
    )
    context.add_init_script(frozen_clock_script(frozen_now_ms))
    return context


def stability_style() -> str:
    """Rendering-stability CSS without any font override — for pages that
    deliberately keep their own bundled typography (e.g. the casino's
    Outfit/Playfair fonts)."""
    return """
    :root,
    body,
    * {
      caret-color: transparent !important;
      -webkit-font-smoothing: none !important;
      -moz-osx-font-smoothing: unset !important;
      font-smooth: never !important;
      text-rendering: geometricPrecision !important;
    }
    *,
    *::before,
    *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      scroll-behavior: auto !important;
    }
    """


def deterministic_style() -> str:
    """`stability_style` plus a hermetic Inter font forced everywhere."""
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
      font-family: "Inter", sans-serif !important;
    }}
    """ + stability_style()
