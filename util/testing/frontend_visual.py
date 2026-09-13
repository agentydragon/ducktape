"""Shared helpers for Python Playwright visual render-health tests.

The Chromium flag set and the frozen-clock init script are single-sourced with
the JS Puppeteer launcher (`frontend_visual/launcher.mjs`): both read
`util/testing/chromium-flags.json` and `util/testing/frozen-clock.js` (kept at
this level — a data file under `frontend_visual/` would shadow this module as
a namespace package), and both resolve the hermetic browser from
`CHROMIUM_HEADLESS_SHELL`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from util.bazel.runfiles import get_required_path, own_repo_rlocation

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright, ViewportSize


_FLAGS = json.loads(get_required_path(own_repo_rlocation("util/testing/chromium-flags.json")).read_text())
# Chromium reads generic-family choices from the profile, not from page CSS. Keep this shared with
# the Puppeteer launcher so the two visual-test stacks exercise the same browser configuration.
_FONT_PREFERENCES = json.loads(
    get_required_path(own_repo_rlocation("util/testing/chromium-font-preferences.json")).read_text()
)
# Makes headless Chromium run in containerized/RBE environments.
CONTAINER_BASE_BROWSER_ARGS: list[str] = _FLAGS["containerBase"]
# Container base plus font/raster/compositing/animation pinning for stable renders.
DETERMINISTIC_BROWSER_ARGS: list[str] = CONTAINER_BASE_BROWSER_ARGS + _FLAGS["deterministicExtra"]


def chromium_executable() -> str | None:
    """The hermetic headless-shell path from `CHROMIUM_HEADLESS_SHELL`, or None
    to fall back to Playwright's own browser resolution (local runs)."""
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    return str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None


def deterministic_browser_context(
    playwright_sync: Playwright,
    *,
    viewport: ViewportSize,
    frozen_now_ms: int,
    color_scheme: Literal["dark", "light", "no-preference", "null"] = "light",
) -> BrowserContext:
    user_data_parent = Path(os.environ.get("TEST_TMPDIR", tempfile.gettempdir()))
    user_data_parent.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(tempfile.mkdtemp(prefix="chrome-user-data-", dir=user_data_parent))
    (user_data_dir / "Default").mkdir()
    (user_data_dir / "Default" / "Preferences").write_text(json.dumps(_FONT_PREFERENCES))
    context = playwright_sync.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=True,
        executable_path=chromium_executable(),
        args=DETERMINISTIC_BROWSER_ARGS,
        viewport=viewport,
        device_scale_factor=1,
        color_scheme=color_scheme,
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="UTC",
    )
    context.add_init_script(frozen_clock_script(frozen_now_ms))
    return context


def frozen_clock_script(now_ms: int) -> str:
    source = get_required_path(own_repo_rlocation("util/testing/frozen-clock.js")).read_text()
    return f"(() => {{ {source} frozenClock({now_ms}); }})();"


def stability_style() -> str:
    """CSS for timing and caret stability; font choice and rasterization are browser-owned."""
    return """
    :root,
    body,
    * {
      caret-color: transparent !important;
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
