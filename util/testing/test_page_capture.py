import io
import re
import struct
import textwrap
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image
from playwright.async_api import Browser, Page

from util.testing.page_capture import (
    DevtoolsViewport,
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


async def test_request_fence_answers_a_served_prefix_and_does_not_count_it_as_an_escape(page: Page) -> None:
    fence = RequestFence(lambda _: False, served_documents={"https://served.test/app/": "<p id='served'>answered</p>"})
    await fence.install(page)
    await page.set_content(
        "<iframe id='framed' src='https://served.test/app/page'></iframe><img src='https://elsewhere.test/x.png'>"
    )

    served = page.frame_locator("#framed").locator("#served")
    await served.wait_for(state="attached")
    assert await served.text_content() == "answered"
    # Only what the prefix does not cover leaves the page.
    with pytest.raises(AssertionError) as escaped:
        fence.assert_none_escaped(context="scene foo")
    assert str(escaped.value) == "scene foo: requests escaped the harness:\n    image https://elsewhere.test/x.png"


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


@pytest.mark.parametrize(
    ("stage", "stall"),
    [
        ("fonts", "Object.defineProperty(document.fonts, 'ready', {value: new Promise(() => {})})"),
        (
            "image decoding",
            """const image = new Image();
            Object.defineProperty(image, 'complete', {value: false});
            image.decode = () => new Promise(() => {});
            document.body.append(image);""",
        ),
        ("paint frames", "window.requestAnimationFrame = () => 0"),
    ],
)
async def test_render_readiness_has_a_deadline_and_names_the_stalled_stage(page: Page, stage: str, stall: str) -> None:
    await page.set_content("<p>ready except for one stalled resource</p>")
    await page.evaluate(f"() => {{ {stall}; }}")
    with pytest.raises(AssertionError, match=f"render readiness: {stage} did not settle within 200ms"):
        await wait_for_stable(page, timeout_ms=200)


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

    with pytest.raises(ValueError, match=r"scene foo: .*has no visible extent"):
        await screenshot_element(page, "#empty", context="scene foo")


async def test_devtools_viewport_captures_the_last_row_as_drawn_and_playwrights_does_not(browser: Browser) -> None:
    # 915 CSS px at a scale of 1.5 is 1372.5 device px, so the last row is half covered. The frame's white
    # border is drawn there at full strength in the capture the compositor makes; Playwright's clipped
    # capture of the same page blends it with the black behind it.
    width, height, scale = 412, 915, 1.5
    async with await browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale
    ) as context:
        page = await context.new_page()
        viewport = await DevtoolsViewport.attach(page, width=width, height=height, device_scale_factor=scale)
        await page.set_content(
            "<body style='margin: 0; background: black'>"
            "<div style='box-sizing: border-box; width: 100vw; height: 100vh; border: 1px solid white'></div>"
        )
        await wait_for_stable(page)

        devtools = Image.open(io.BytesIO(await viewport.screenshot())).convert("RGB")
        playwright = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")

    assert devtools.size == playwright.size == (618, 1373)
    last_row = (0, 1372, 618, 1373)
    assert set(devtools.crop(last_row).getdata()) == {(255, 255, 255)}
    assert set(playwright.crop(last_row).getdata()) != {(255, 255, 255)}


if __name__ == "__main__":
    pytest_bazel.main()
