"""Shared helpers for Python Playwright visual render-health tests.

The Chromium flag set and the frozen-clock init script are single-sourced with
the JS Puppeteer launcher (`frontend_visual/launcher.mjs`): both read
`util/testing/chromium-flags.json` and `util/testing/frozen-clock.js` (kept at
this level — a data file under `frontend_visual/` would shadow this module as
a namespace package), and both drive the hermetic `@chrome_headless_shell` browser. This
module finds its binary in the runfiles, where `browser_launcher_assets` puts it; the JS launcher
is handed the path in `CHROMIUM_HEADLESS_SHELL`.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from util.bazel.runfiles import find_path, get_required_path, own_repo_rlocation

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright, ViewportSize


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


# The instant the scenario sweep freezes page clocks to, so date-relative text renders the same on
# every run. 2025-02-01T12:00:00Z, as FROZEN_NOW_MS in frontend_visual/launcher.mjs, which the
# Puppeteer sweeps use until their lanes move over.
FROZEN_NOW_MS = 1_738_411_200_000


def chromium_executable() -> str | None:
    """The hermetic headless-shell executable in this target's runfiles, or None
    to fall back to Playwright's own browser resolution."""
    return str(path) if (path := find_path("chrome_headless_shell/chrome-headless-shell")) else None


def _font_pinned_user_data_dir() -> Path:
    user_data_parent = Path(os.environ.get("TEST_TMPDIR", tempfile.gettempdir()))
    user_data_parent.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(tempfile.mkdtemp(prefix="chrome-user-data-", dir=user_data_parent))
    (user_data_dir / "Default").mkdir()
    (user_data_dir / "Default" / "Preferences").write_text(json.dumps(_FONT_PREFERENCES))
    return user_data_dir


async def deterministic_browser_context(
    playwright: Playwright,
    *,
    viewport: ViewportSize,
    frozen_now_ms: int,
    color_scheme: Literal["dark", "light", "no-preference", "null"] = "light",
    device_scale_factor: float = 1,
    has_touch: bool = False,
    extra_args: Sequence[str] = (),
) -> BrowserContext:
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(_font_pinned_user_data_dir()),
        headless=True,
        executable_path=chromium_executable(),
        args=[*DETERMINISTIC_BROWSER_ARGS, *extra_args],
        viewport=viewport,
        device_scale_factor=device_scale_factor,
        has_touch=has_touch,
        color_scheme=color_scheme,
        reduced_motion="reduce",
        locale="en-US",
        timezone_id="UTC",
    )
    await context.add_init_script(frozen_clock_script(frozen_now_ms))
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
