"""Verify the Playwright launcher pins Chromium's generic font families."""

from __future__ import annotations

import pytest
import pytest_bazel
from playwright.async_api import Page, Playwright

from util.testing.frontend_visual import (
    DISABLE_ANIMATIONS_CSS,
    deterministic_browser_context,
    disable_animations_script,
)
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)


@pytest.fixture
def los_angeles_process_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Before `playwright` starts the driver, whose environment the browser inherits.
    monkeypatch.setenv("TZ", "America/Los_Angeles")


async def _platform_families(page: Page, selector: str) -> list[str]:
    session = await page.context.new_cdp_session(page)
    await session.send("DOM.enable")
    await session.send("CSS.enable")
    root = (await session.send("DOM.getDocument"))["root"]
    node_id = (await session.send("DOM.querySelector", {"nodeId": root["nodeId"], "selector": selector}))["nodeId"]
    fonts = (await session.send("CSS.getPlatformFontsForNode", {"nodeId": node_id}))["fonts"]
    await session.detach()
    return [font["familyName"] for font in fonts]


async def test_generic_families_are_browser_pinned(playwright: Playwright) -> None:
    async with deterministic_browser_context(
        playwright, viewport={"width": 800, "height": 600}, frozen_now_ms=0
    ) as context:
        page = await context.new_page()
        await page.set_content(
            """
            <style>
              #serif { font-family: serif; }
              #sans { font-family: sans-serif; }
              #mono { font-family: monospace; }
              #system { font-family: system-ui; }
              #explicit { font-family: "Liberation Mono"; }
            </style>
            <div id="serif">Serif sample</div>
            <div id="sans">Sans sample</div>
            <div id="mono">Mono sample</div>
            <div id="system">System sample</div>
            <pre id="pre"><code id="code">const answer = 42;</code></pre>
            <div id="explicit">Explicit sample</div>
            """
        )
        for selector, family in (
            ("#serif", "Liberation Serif"),
            ("#sans", "Liberation Sans"),
            ("#mono", "Liberation Mono"),
            ("#system", "Liberation Sans"),
            ("#pre", "Liberation Mono"),
            ("#code", "Liberation Mono"),
            ("#explicit", "Liberation Mono"),
        ):
            families = await _platform_families(page, selector)
            assert family in families, f"{selector} used {families}, expected {family}"


@pytest.mark.usefixtures("los_angeles_process_timezone")
async def test_the_page_timezone_is_utc_whatever_the_process_timezone(playwright: Playwright) -> None:
    async with deterministic_browser_context(
        playwright, viewport={"width": 800, "height": 600}, frozen_now_ms=0
    ) as context:
        page = await context.new_page()
        # July, when Los Angeles is 420 minutes behind UTC.
        timezone = await page.evaluate(
            "[Intl.DateTimeFormat().resolvedOptions().timeZone, new Date(2025, 6, 1).getTimezoneOffset()]"
        )

    assert timezone == ["UTC", 0]


async def test_animations_and_transitions_are_pinned_by_the_css(playwright: Playwright) -> None:
    async with deterministic_browser_context(
        playwright, viewport={"width": 800, "height": 600}, frozen_now_ms=0
    ) as context:
        page = await context.new_page()
        await page.set_content(
            f"""
            <style>
              @keyframes pulse {{ to {{ opacity: 0.5; }} }}
              #animated {{ animation: pulse 1s linear infinite; }}
              #transitioned {{ transition: opacity 5s; }}
              {DISABLE_ANIMATIONS_CSS}
            </style>
            <div id="animated">a</div>
            <div id="transitioned">t</div>
            """
        )

        styles = await page.evaluate(
            """() => ({
                playState: getComputedStyle(document.getElementById("animated")).animationPlayState,
                transition: getComputedStyle(document.getElementById("transitioned")).transitionProperty,
            })"""
        )

    assert styles == {"playState": "paused", "transition": "none"}


async def test_animations_script_pins_a_page_that_mounts_after_it(playwright: Playwright) -> None:
    async with deterministic_browser_context(
        playwright, viewport={"width": 800, "height": 600}, frozen_now_ms=None
    ) as context:
        await context.add_init_script(disable_animations_script())
        page = await context.new_page()
        await page.set_content(
            """
            <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
            <div style="animation: spin 1s linear infinite">spinner</div>
            """
        )

        play_states = await page.evaluate("document.getAnimations().map(animation => animation.playState)")

    assert play_states == ["paused"]


async def test_visual_page_refuses_a_page_from_another_launch(page: Page) -> None:
    # `page` is util.playwright's: container flags only, none of the render pinning.
    with pytest.raises(ValueError, match="deterministic_browser_context"):
        VisualPage(page, output_dir=undeclared_outputs_dir(), title="Not pinned")


if __name__ == "__main__":
    pytest_bazel.main()
