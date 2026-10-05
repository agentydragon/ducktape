import re
import struct
import textwrap
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Page

from util.testing.page_capture import (
    PageErrors,
    RequestFence,
    assert_network_settled,
    screenshot_element,
    wait_for_stable,
)

# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)


async def test_page_errors_name_what_the_page_threw(page: Page) -> None:
    errors = PageErrors(page)
    errors.assert_none(context="scene foo")

    async with page.expect_event("pageerror"):
        await page.set_content("<script>throw new Error('boom from the scene')</script>")

    with pytest.raises(AssertionError, match=r"scene foo: uncaught page errors:\n\s+Error: boom from the scene"):
        errors.assert_none(context="scene foo")


async def test_request_fence_aborts_what_it_does_not_allow_and_names_it(page: Page, tmp_path: Path) -> None:
    fence = RequestFence(lambda request: request.url.startswith("file://"))
    await fence.install(page)
    fence.assert_none_escaped(context="scene foo")
    (tmp_path / "local.js").write_text("window.localLoaded = true;")
    (tmp_path / "index.html").write_text(
        textwrap.dedent(
            """\
            <script src="local.js"></script>
            <script src="http://fenced.test/remote.js"></script>
            <img src="data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA=">
            """
        )
    )

    await page.goto((tmp_path / "index.html").as_uri(), wait_until="load")

    assert await page.evaluate("window.localLoaded") is True
    with pytest.raises(AssertionError) as escaped:
        fence.assert_none_escaped(context="scene foo")
    # The allowed and the in-page requests are not holes; the one that left the page is.
    assert str(escaped.value) == "scene foo: requests escaped the harness:\n    script http://fenced.test/remote.js"


async def test_network_settled_passes_a_page_with_no_ledger(page: Page) -> None:
    await page.set_content("<p>no stubbed fetch here</p>")

    await assert_network_settled(page, context="scene foo")


async def test_network_settled_waits_for_a_fetch_that_starts_another(page: Page) -> None:
    # The first response lands while the re-render it causes has not yet started its own fetch.
    await page.set_content(
        textwrap.dedent(
            """\
            <script>
              const ledger = (window.__visualNetworkLedger__ = { pending: ["/api/a"], violations: [] });
              setTimeout(() => {
                ledger.pending.length = 0;
                requestAnimationFrame(() => {
                  ledger.pending.push("/api/b");
                  setTimeout(() => { ledger.pending.length = 0; window.drained = true; }, 50);
                });
              }, 50);
            </script>
            """
        )
    )

    await assert_network_settled(page, context="scene foo")

    assert await page.evaluate("window.drained") is True


async def test_network_settled_names_what_is_still_in_flight(page: Page) -> None:
    await page.set_content(
        "<script>window.__visualNetworkLedger__ = { pending: ['/api/a', '/api/b'], violations: [] };</script>"
    )

    with pytest.raises(
        AssertionError, match=re.escape("scene foo: requests still in flight after 200ms: /api/a, /api/b")
    ):
        await assert_network_settled(page, context="scene foo", timeout_ms=200)


async def test_network_settled_fails_on_what_the_ledger_recorded(page: Page) -> None:
    await page.set_content(
        "<script>window.__visualNetworkLedger__ = { pending: [], violations: ['unmatched route /api/x'] };</script>"
    )

    with pytest.raises(AssertionError, match=r"scene foo: network violations:\n\s+unmatched route /api/x"):
        await assert_network_settled(page, context="scene foo")


async def test_stable_does_not_wait_on_an_image_that_cannot_decode(page: Page) -> None:
    # A broken src rejects decode(); that is the page's problem to render, not a reason to hang or fail.
    await page.set_content('<img src="data:image/png;base64,AAAA">')

    await wait_for_stable(page)


async def test_screenshot_names_the_selector_that_matched_nothing(page: Page) -> None:
    await page.set_content("<div id='present'>x</div>")

    assert (await screenshot_element(page, "#present", context="scene foo")).startswith(b"\x89PNG")
    with pytest.raises(LookupError, match=re.escape("scene foo: selector='#absent' matched no element")):
        await screenshot_element(page, "#absent", context="scene foo")


@pytest.mark.parametrize(("css_height", "rows"), [("40.4px", 40), ("40.5px", 41), ("2000px", 2000)])
async def test_screenshot_rounds_to_the_nearest_pixel_and_takes_the_whole_element(
    page: Page, css_height: str, rows: int
) -> None:
    await page.set_viewport_size({"width": 300, "height": 200})
    await page.set_content(f"<body style='margin: 0'><div id='el' style='width: 100px; height: {css_height}'></div>")

    png = await screenshot_element(page, "#el", context="scene foo")

    assert struct.unpack(">II", png[16:24]) == (100, rows)


async def test_screenshot_refuses_an_element_with_no_extent(page: Page) -> None:
    await page.set_content("<div id='empty'></div>")

    with pytest.raises(ValueError, match=r"scene foo: selector='#empty' has no visible extent"):
        await screenshot_element(page, "#empty", context="scene foo")


if __name__ == "__main__":
    pytest_bazel.main()
