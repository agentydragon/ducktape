"""Verify the Playwright launcher pins Chromium's generic font families."""

from __future__ import annotations

import pytest_bazel

from util.testing.frontend_visual import deterministic_browser_context

# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)


def _platform_families(page, selector: str) -> list[str]:
    session = page.context.new_cdp_session(page)
    session.send("DOM.enable")
    session.send("CSS.enable")
    root = session.send("DOM.getDocument")["root"]
    node_id = session.send("DOM.querySelector", {"nodeId": root["nodeId"], "selector": selector})["nodeId"]
    fonts = session.send("CSS.getPlatformFontsForNode", {"nodeId": node_id})["fonts"]
    session.detach()
    return [font["familyName"] for font in fonts]


def test_generic_families_are_browser_pinned(playwright_sync) -> None:
    context = deterministic_browser_context(
        playwright_sync,
        viewport={"width": 800, "height": 600},
        frozen_now_ms=0,
    )
    try:
        page = context.new_page()
        page.set_content(
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
            families = _platform_families(page, selector)
            assert family in families, f"{selector} used {families}, expected {family}"
        page.close()
    finally:
        context.close()


if __name__ == "__main__":
    pytest_bazel.main()
