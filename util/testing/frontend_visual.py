"""Shared helpers for Python Playwright visual-golden tests."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from util.bazel.runfiles import get_required_path

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright, ViewportSize


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


def launch_deterministic_browser(playwright_sync: Playwright) -> Browser:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    return playwright_sync.chromium.launch(headless=True, executable_path=executable, args=DETERMINISTIC_BROWSER_ARGS)


def frozen_clock_script(now_ms: int) -> str:
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
        }})({now_ms});
        """


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


def deterministic_style() -> str:
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
