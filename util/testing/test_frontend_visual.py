"""Verify the Playwright launcher pins Chromium's generic font families."""

from __future__ import annotations

import pytest
import pytest_bazel
from playwright.async_api import Page, Playwright

from util.testing.frontend_visual import DISABLE_ANIMATIONS_CSS, deterministic_browser_context

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
    async with await deterministic_browser_context(
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
    async with await deterministic_browser_context(
        playwright, viewport={"width": 800, "height": 600}, frozen_now_ms=0
    ) as context:
        page = await context.new_page()
        # July, when Los Angeles is 420 minutes behind UTC.
        timezone = await page.evaluate(
            "[Intl.DateTimeFormat().resolvedOptions().timeZone, new Date(2025, 6, 1).getTimezoneOffset()]"
        )

    assert timezone == ["UTC", 0]


async def test_animations_and_transitions_are_pinned_by_the_css(playwright: Playwright) -> None:
    async with await deterministic_browser_context(
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


if __name__ == "__main__":
    pytest_bazel.main()
