"""Shared Action payload surfaces and bounded editor retention."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from util.testing.viewports import DESKTOP, Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@DESKTOP_AND_MOBILE
async def test_action_payloads_share_operation_surfaces(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.mount_operation_presentation()
    panel = view.page.locator("#operation-presentation-surface")
    manifest_heading = panel.locator("[data-operation-heading]:visible").filter(
        has_text=re.compile(r"^Applied manifest$")
    )
    await expect(manifest_heading).to_be_visible()
    await expect(panel.get_by_role("button", name="Show all 24 lines")).to_be_visible()
    await expect(panel.get_by_text("Recorded data", exact=True)).to_be_visible()
    await expect(panel.locator(".agentplane-output-disclosure .cm-line").first).to_have_text(
        "hello from the selected container"
    )
    await expect(panel.locator(".cm-ui-special-char")).to_contain_text("ZWSP")

    sections = await panel.locator(".agentplane-operation-section").evaluate_all(
        "elements => elements.map(element => {"
        "const style = getComputedStyle(element);"
        "const code = element.querySelector('.ui-code-block');"
        "return {padding: style.paddingLeft, border: style.borderLeftWidth, codeBorder: code && getComputedStyle(code).borderLeftWidth};"
        "})"
    )
    assert len(sections) == 5
    assert all(section["padding"] != "0px" for section in sections)
    border_widths = [float(section["border"].removesuffix("px")) for section in sections]
    framed_widths = [border_widths[index] for index in (0, 2, 3, 4)]
    assert border_widths[1] == 0
    assert all(width > 0 for width in framed_widths)
    assert max(framed_widths) - min(framed_widths) <= 0.01
    assert all(section["codeBorder"] in (None, "0px") for section in sections)
    await view.capture(target=panel, name=f"action_operation_surfaces_{viewport.width}")

    output = panel.locator(".agentplane-output-disclosure")
    toggle = output.get_by_role("button").first
    editor = output.locator(".ui-code-block-container")
    await expect(toggle).to_have_attribute("aria-expanded", "true")
    await toggle.click()
    await expect(toggle).to_have_attribute("aria-expanded", "false")
    await expect(editor).to_have_count(0)
    await expect(output.locator(".ui-code-block-placeholder")).to_be_attached()

    await view.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await expect(editor).to_have_count(0)
    await view.page.evaluate("() => window.scrollTo(0, 0)")
    await toggle.click()
    await expect(toggle).to_have_attribute("aria-expanded", "true")
    await expect(editor).to_be_attached()
    await expect(editor.locator(".cm-line").first).to_have_text("hello from the selected container")
    lines = await editor.locator(".cm-line").all_text_contents()
    assert lines == ["hello from the selected container", ""]
    selected = await editor.locator(".cm-line").first.evaluate(
        "line => {"
        "const selection = window.getSelection();"
        "const range = document.createRange();"
        "range.selectNodeContents(line);"
        "selection?.removeAllRanges();"
        "selection?.addRange(range);"
        "return selection?.toString();"
        "}"
    )
    assert selected == "hello from the selected container"


@pytest.mark.parametrize("viewport", [DESKTOP], ids=["desktop"])
async def test_action_code_releases_and_remounts_with_current_width(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.mount_operation_presentation()
    page = view.page
    sample = page.locator("#parked-code-section")
    editor = sample.locator(".ui-code-block-container")
    await sample.scroll_into_view_if_needed()
    await expect(editor.locator(".ui-code-block")).to_be_attached()
    initial_height = await editor.evaluate("element => element.getBoundingClientRect().height")

    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    placeholder = sample.locator(".ui-code-block-placeholder")
    await expect(placeholder).to_be_attached()
    parked_height = await placeholder.evaluate("element => element.getBoundingClientRect().height")
    assert abs(parked_height - initial_height) <= 1

    await page.set_viewport_size({"width": 360, "height": viewport.height})
    await expect(placeholder).to_be_attached()
    assert await placeholder.evaluate("element => element.getBoundingClientRect().height") == parked_height
    await sample.scroll_into_view_if_needed()
    await expect(editor.locator(".ui-code-block")).to_be_attached()
    resized_height = await editor.evaluate("element => element.getBoundingClientRect().height")
    assert resized_height > initial_height + 100

    first_line = editor.locator(".cm-line").first
    selected = await first_line.evaluate(
        "line => {"
        "const selection = window.getSelection();"
        "const range = document.createRange();"
        "range.selectNodeContents(line);"
        "selection?.removeAllRanges();"
        "selection?.addRange(range);"
        "return selection?.toString();"
        "}"
    )
    assert selected == await first_line.text_content()


if __name__ == "__main__":
    pytest_bazel.main()
