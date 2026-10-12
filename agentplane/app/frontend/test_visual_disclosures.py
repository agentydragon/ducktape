"""Agentplane disclosures visual behavior tests."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.visual_assertions import (
    _INNER,
    _MAIN,
    _OUTER,
    _OUTPUT,
    _SCROLL,
    _box,
    _expect_at,
    _expect_released,
    _focus,
    _heading_top,
    _height,
    _in_viewport,
    _open_tool_run,
    _scroll_to,
    _scroll_to_copy,
)
from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_collapsed_disclosure(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(open=False)
    await view.page.wait_for_selector(
        ".demo-main .agentplane-disclosure-summary[aria-expanded='false']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    await expect(page.locator(_MAIN)).to_have_attribute("data-expanded", "false")
    assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") == 0
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_short_disclosure_fits(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(short=True)
    await view.page.wait_for_selector(
        ".demo-main .agentplane-disclosure-summary[aria-expanded='true']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    assert not await page.locator(_SCROLL).evaluate("element => element.scrollHeight > element.clientHeight")
    await expect(page.locator(_MAIN)).to_have_attribute("data-expanded", "true")
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_long_disclosure_before_sticking(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure()
    await view.page.wait_for_selector(
        ".demo-main .agentplane-disclosure-summary[aria-expanded='true']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    assert await page.locator(_SCROLL).evaluate("element => element.scrollHeight > element.clientHeight")
    assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") == 0
    top = await _heading_top(page, _MAIN)
    viewport = await _box(page, _SCROLL)
    assert 0 < top < viewport["height"] * 0.6
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_long_disclosure_sticks(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure()
    await view.page.wait_for_selector(
        ".demo-main .agentplane-disclosure-heading[data-expanded='true']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    await _scroll_to_copy(page, "long-paragraph", 64)
    await _expect_at(page, _MAIN, 0)
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_disclosure_releases_after_content(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(following_section=True)
    await view.page.wait_for_selector("[data-demo-target='following-disclosure']", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _scroll_to_copy(page, "following-disclosure", 64)
    await _expect_released(page, _MAIN)
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_nested_parent_sticks_before_child(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    outer_height = await _height(page, _OUTER)
    await _scroll_to_copy(page, "outer-paragraph", outer_height + 8)
    await _expect_at(page, _OUTER, 0)
    inner_top = await _heading_top(page, _INNER)
    viewport = await _box(page, _SCROLL)
    assert outer_height + 4 < inner_top < viewport["height"]
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
@pytest.mark.parametrize("wrapped_headings", [False, True], ids=["short", "wrapped"])
async def test_nested_headings_stack(view: VisualPage, app: AgentplaneFixture, wrapped_headings: bool) -> None:
    await app.mount_disclosure(nested=True, wrapped_headings=wrapped_headings)
    await view.page.wait_for_selector(
        ".demo-outer .agentplane-disclosure-heading[data-expanded='true']", state="attached"
    )
    await view.page.wait_for_selector(
        ".demo-inner .agentplane-disclosure-heading[data-expanded='true']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    outer_height = await _height(page, _OUTER)
    inner_height = await _height(page, _INNER)
    await _scroll_to_copy(page, "nested-paragraph", outer_height + inner_height + 16)
    await _expect_at(page, _OUTER, 0)
    await _expect_at(page, _INNER, outer_height)
    if wrapped_headings:
        assert outer_height >= 56, "outer mobile heading did not wrap"
        assert inner_height >= 56, "inner mobile heading did not wrap"
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_nested_child_releases_behind_parent(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True)
    await view.page.wait_for_selector(
        ".demo-outer .agentplane-disclosure-heading[data-expanded='true']", state="attached"
    )
    await view.check(context="fixture ready")
    page = view.page
    outer_height = await _height(page, _OUTER)
    await _scroll_to_copy(page, "following-nested", outer_height + 16)
    await _expect_at(page, _OUTER, 0)
    outer_box, inner_box = (await _box(page, _OUTER), await _box(page, _INNER))
    outer_z = int(await page.locator(_OUTER).evaluate("element => getComputedStyle(element).zIndex"))
    inner_z = int(await page.locator(_INNER).evaluate("element => getComputedStyle(element).zIndex"))
    assert inner_box["y"] + inner_box["height"] <= outer_box["y"] + outer_box["height"] + 1
    assert outer_z > inner_z
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_nested_parent_releases_after_content(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _scroll_to_copy(page, "following-outer", 64)
    await _expect_released(page, _INNER)
    await _expect_released(page, _OUTER)
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_output_heading_stacks_below_tool(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True, tool_output=True, after_output=False)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-output .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    outer_height, inner_height, output_height = (
        await _height(page, _OUTER),
        await _height(page, _INNER),
        await _height(page, _OUTPUT),
    )
    await _scroll_to_copy(page, "output-paragraph", outer_height + inner_height + output_height + 16)
    await _expect_at(page, _OUTER, 0)
    await _expect_at(page, _INNER, outer_height)
    await _expect_at(page, _OUTPUT, outer_height + inner_height)
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_output_heading_enters_below_tool(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True, tool_output=True)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-output .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    outer_height, inner_height = (await _height(page, _OUTER), await _height(page, _INNER))
    await _scroll_to_copy(page, "before-output", outer_height + inner_height + 16)
    await _expect_at(page, _OUTER, 0)
    await _expect_at(page, _INNER, outer_height)
    output_top = await _heading_top(page, _OUTPUT)
    viewport = await _box(page, _SCROLL)
    assert outer_height + inner_height + 4 < output_top < viewport["height"]
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_collapsed_output_keeps_its_sticky_slot(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True, tool_output=True, output_open=False, before_output=False)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-output .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    outer_height, inner_height = (await _height(page, _OUTER), await _height(page, _INNER))
    await _scroll_to(page, _OUTPUT, outer_height + inner_height)
    await _expect_at(page, _OUTER, 0)
    await _expect_at(page, _INNER, outer_height)
    await _expect_at(page, _OUTPUT, outer_height + inner_height)
    await expect(page.locator(_OUTPUT)).to_have_attribute("data-expanded", "false")
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_output_heading_releases_after_content(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_disclosure(nested=True, tool_output=True)
    await view.page.wait_for_selector(".demo-outer .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-inner .agentplane-disclosure-heading", state="attached")
    await view.page.wait_for_selector(".demo-output .agentplane-disclosure-heading", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    outer_height, inner_height = (await _height(page, _OUTER), await _height(page, _INNER))
    await _scroll_to_copy(page, "following-output", outer_height + inner_height + 16)
    await _expect_at(page, _OUTER, 0)
    await _expect_at(page, _INNER, outer_height)
    await _expect_released(page, _OUTPUT)
    await view.capture()


@pytest.mark.parametrize(
    ("state", "viewport"),
    [
        ("collapsed-hover", DESKTOP),
        ("collapsed-hover", MOBILE),
        ("expanded-hover", DESKTOP),
        ("expanded-hover", MOBILE),
        ("tool-hover", DESKTOP),
        ("tool-hover", MOBILE),
        ("expanded-focus", DESKTOP),
        ("expanded-focus", MOBILE),
    ],
    ids=[
        "collapsed-hover-desktop",
        "collapsed-hover-mobile",
        "expanded-hover-desktop",
        "expanded-hover-mobile",
        "tool-hover-desktop",
        "tool-hover-mobile",
        "expanded-focus-desktop",
        "expanded-focus-mobile",
    ],
)
async def test_disclosure_control_reaches_card_edges(view: VisualPage, app: AgentplaneFixture, state: str) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    if state != "collapsed-hover":
        await _open_tool_run(page)
    if state == "tool-hover":
        control = (
            page.locator(".agentplane-step-details")
            .filter(has=page.locator(".agentplane-step-title:text-is('Bash')"))
            .locator(".agentplane-disclosure-summary")
            .first
        )
    else:
        control = page.locator(".agentplane-disclosure-summary").filter(has_text="tool calls").first
    if state == "tool-hover":
        await _focus(page, control)
    else:
        # A sticky control can already be visible while its card's top is far above the
        # viewport. Show the card's real rounded corners for this coverage check and capture.
        await page.locator('[aria-label="Thread history"]').evaluate("el => { el.scrollTop = 0; }")
        await wait_for_stable(page)
        await _in_viewport(control)
    card = control.locator("xpath=ancestor::*[@data-open][1]")
    control_box = await control.bounding_box()
    card_box = await card.bounding_box()
    assert control_box is not None
    assert card_box is not None
    # Allow the card's one-pixel border. The top and both sides belong to the actual control,
    # including the padding around its rectangular hover fill.
    assert abs(control_box["x"] - card_box["x"]) <= 1
    assert abs(control_box["y"] - card_box["y"]) <= 1
    assert abs(control_box["x"] + control_box["width"] - card_box["x"] - card_box["width"]) <= 1
    assert await control.evaluate("el => parseFloat(getComputedStyle(el).borderTopLeftRadius)") > 0
    assert await control.evaluate(
        "el => {\n                const r = el.getBoundingClientRect();\n                return [[r.x + 5, r.y + 2], [r.right - 5, r.y + 2]].every(([x, y]) =>\n                    el.contains(document.elementFromPoint(x, y)));\n            }"
    )
    if state == "expanded-focus":
        await control.focus()
        await page.keyboard.press("Tab")
        await page.keyboard.press("Shift+Tab")
        await expect(control).to_be_focused()
        assert await control.evaluate("el => el.matches(':focus-visible')")
    else:
        await control.hover(position={"x": 5, "y": 2})
        assert await control.evaluate("el => el.matches(':hover')")
    await view.capture(target=view.page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
