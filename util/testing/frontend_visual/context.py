"""Deterministic Playwright contexts for visual tests."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.browser.launcher import DETERMINISTIC_BROWSER_ARGS, chromium_executable

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright, ViewportSize


# Chromium reads generic-family choices from the profile, not from page CSS.
_FONT_PREFERENCES = json.loads(
    get_required_path(own_repo_rlocation("util/testing/frontend_visual/chromium-font-preferences.json")).read_text()
)
# Hard-pins every animation and transition to its first frame, for a page that inlines its own
# `<style>`. `reduced_motion="reduce"` is not enough: it only helps component CSS that checks that
# media feature, and some does not (Mantine's `Indicator processing` ping is an unconditional
# `animation: … 1000ms linear infinite`). An animation like that keeps running on the compositor's own
# clock whatever the frozen `Date`, so the frame captured depends on scheduling jitter. It must be in
# the page before the animated element mounts: `animation-play-state: paused` pins an animation to its
# start frame only when it is in effect at creation; applied later it freezes whatever frame the
# animation had reached by then.
DISABLE_ANIMATIONS_CSS = get_required_path(
    own_repo_rlocation("util/testing/frontend_visual/disable-animations.css")
).read_text()


# The instant visual harnesses freeze page clocks to, so date-relative text renders the same on
# every run. 2025-02-01T12:00:00Z.
FROZEN_NOW_MS = 1_738_411_200_000


def _font_pinned_profile() -> tempfile.TemporaryDirectory[str]:
    user_data_parent = Path(os.environ.get("TEST_TMPDIR", tempfile.gettempdir()))
    user_data_parent.mkdir(parents=True, exist_ok=True)
    profile = tempfile.TemporaryDirectory(prefix="chrome-user-data-", dir=user_data_parent)
    try:
        user_data_dir = Path(profile.name)
        (user_data_dir / "Default").mkdir()
        (user_data_dir / "Default" / "Preferences").write_text(json.dumps(_FONT_PREFERENCES))
    except OSError:
        profile.cleanup()
        raise
    return profile


@asynccontextmanager
async def deterministic_browser_context(
    playwright: Playwright,
    *,
    viewport: ViewportSize,
    frozen_now_ms: int | None,
    color_scheme: Literal["dark", "light", "no-preference", "null"] = "light",
    device_scale_factor: float = 1,
    has_touch: bool = False,
    extra_args: Sequence[str] = (),
) -> AsyncIterator[BrowserContext]:
    """Own the browser and its font-pinned profile, including failed setup.

    `frozen_now_ms=None` leaves the page clock live, for a page whose server-driven behavior runs on real time.
    """
    profile = await asyncio.to_thread(_font_pinned_profile)
    try:
        async with await playwright.chromium.launch_persistent_context(
            user_data_dir=profile.name,
            headless=True,
            executable_path=chromium_executable(),
            # Emulation alone leaves the native widget at scale 1. CDP screenshots then
            # rescale the compositor to device_scale_factor and back, racing raster updates
            # (SVG edges varied even across captures of an unchanged page). Paint at the
            # capture scale from launch so screenshot capture needs no scale transition.
            args=[*DETERMINISTIC_BROWSER_ARGS, f"--force-device-scale-factor={device_scale_factor}", *extra_args],
            viewport=viewport,
            device_scale_factor=device_scale_factor,
            has_touch=has_touch,
            color_scheme=color_scheme,
            reduced_motion="reduce",
            locale="en-US",
            timezone_id="UTC",
        ) as context:
            if frozen_now_ms is not None:
                await context.add_init_script(frozen_clock_script(frozen_now_ms))
            yield context
    finally:
        await asyncio.to_thread(profile.cleanup)


def disable_animations_script() -> str:
    """Init script putting `DISABLE_ANIMATIONS_CSS` in effect before the page's own elements mount.

    For a page served by a real server, which cannot inline the stylesheet the way the inline harness does.
    An adopted stylesheet rather than a `<style>` element, because the document has no `<head>` yet.
    """
    return (
        f"(() => {{ const sheet = new CSSStyleSheet(); sheet.replaceSync({json.dumps(DISABLE_ANIMATIONS_CSS)}); "
        "document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet]; })();"
    )


def frozen_clock_script(now_ms: int) -> str:
    source = get_required_path(own_repo_rlocation("util/testing/frontend_visual/frozen-clock.js")).read_text()
    return f"(() => {{ {source} frozenClock({now_ms}); }})();"
