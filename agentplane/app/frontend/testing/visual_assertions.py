"""Shared Agentplane layout assertions and browser interaction helpers."""

import json
import re
from textwrap import dedent

from playwright.async_api import FloatRect, Locator, Page, expect

from util.testing.page_capture import wait_for_stable
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

_SCROLL = "[data-disclosure-demo-scroll]"


_HEADING = ".agentplane-disclosure-heading"


_MAIN = f".demo-main .agentplane-disclosure-item > {_HEADING}"


_OUTER = f".demo-outer > .agentplane-disclosure-item > {_HEADING}"


_INNER = f".demo-inner > .agentplane-disclosure-item > {_HEADING}"


_OUTPUT = f".demo-output > .agentplane-disclosure-item > {_HEADING}"


async def _in_viewport(target: Locator) -> None:
    await expect(target).to_be_visible()
    await expect(target).to_be_in_viewport(ratio=1)


async def _focus(page: Page, target: Locator) -> None:
    await target.evaluate("element => element.scrollIntoView({ block: 'center', inline: 'nearest' })")
    await wait_for_stable(page)
    await _in_viewport(target)


async def _open_raw_switches(page: Page) -> None:
    switches = page.locator("label").filter(has_text=re.compile(r"^Raw$"))
    await expect(switches.first).to_be_visible()
    count = await switches.count()
    assert count > 0
    for index in range(count):
        await switches.nth(index).click()
    await expect(page.locator('input[type="checkbox"]:checked').first).to_be_attached()


async def _select_reconnect(page: Page) -> None:
    connection = page.locator('select[name="connection"]')
    await connection.select_option(index=1)
    await page.locator('select[name="service_account"]').select_option(label="agentplane-visual/operator-assistant")
    await expect(page.locator("[data-reconnect-review]")).to_be_visible()


async def _open_run(page: Page) -> None:
    run = (
        page.locator(".agentplane-disclosure-summary[aria-expanded='false']")
        .filter(has_text=re.compile("tool call", re.IGNORECASE))
        .first
    )
    await run.click()
    await expect(page.locator(".agentplane-step-details").first).to_be_visible()


async def _rollout_start(view: VisualPage) -> None:
    page = view.page
    history = page.get_by_role("region", name="Thread history")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.evaluate("element => { element.scrollTop = 0; }")
    await wait_for_stable(page)
    await _rollout_geometry(view, "overview")


async def _rollout_run(view: VisualPage, position: str) -> None:
    page = view.page
    await _rollout_start(view)
    await _open_run(page)
    steps = page.locator(".agentplane-run-steps").first
    if position == "start":
        static_title = await steps.locator(".agentplane-step-static .agentplane-step-title").first.bounding_box()
        disclosure_title = await steps.locator(".agentplane-step-details .agentplane-step-title").first.bounding_box()
        assert static_title is not None
        assert disclosure_title is not None
        assert abs(static_title["x"] - disclosure_title["x"]) <= 1, "plain and expandable steps must align"
    target = steps.locator(":scope > *").first if position == "start" else steps.locator(":scope > *").last
    await _focus(page, target)
    await target.hover()
    await _rollout_geometry(view, f"run-{position}")


async def _rollout_geometry(view: VisualPage, state: str) -> None:
    page = view.page
    assert view.capture_name is not None, "rollout diagnostics need a test case identity"
    geometry = await page.evaluate(
        dedent("""() => {
      const box = element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
          clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
          scrollLeft: element.scrollLeft, paddingLeft: style.paddingLeft, paddingRight: style.paddingRight };
      };
      const history = document.querySelector('[aria-label="Thread history"]');
      return {
        shell: box(document.querySelector('.agentplane-shell-main-content')),
        history: box(history),
        rows: [...history.querySelectorAll('[data-thread-anchor]')].map(box)
      };
    }""")
    )
    (undeclared_outputs_dir() / f"{view.capture_name}-{state}-geometry.json").write_text(json.dumps(geometry, indent=2))


async def _box(page: Page, selector: str) -> FloatRect:
    box = await page.locator(selector).bounding_box()
    assert box is not None, f"missing box for {selector}"
    return box


async def _height(page: Page, selector: str) -> float:
    return (await _box(page, selector))["height"]


async def _scroll_to(page: Page, selector: str, screen_top: float) -> None:
    await page.locator(selector).evaluate(
        """(element, screenTop) => {
            const viewport = element.closest('[data-disclosure-demo-scroll]');
            if (!viewport) throw new Error('disclosure scroll viewport is missing');
            viewport.scrollTop = 0;
            const top = element.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
            viewport.scrollTop = Math.max(0, top - screenTop);
        }""",
        screen_top,
    )
    await wait_for_stable(page)
    assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") > 0


async def _scroll_to_copy(page: Page, target: str, screen_top: float) -> None:
    await _scroll_to(page, f'[data-demo-target="{target}"]', screen_top)


async def _heading_top(page: Page, selector: str) -> float:
    return (await _box(page, selector))["y"] - (await _box(page, _SCROLL))["y"]


async def _expect_at(page: Page, selector: str, offset: float) -> None:
    actual = await _heading_top(page, selector)
    assert abs(actual - offset) <= 4, f"{selector} at {actual}px; expected {offset}px"


async def _expect_released(page: Page, selector: str) -> None:
    box = await _box(page, selector)
    viewport = await _box(page, _SCROLL)
    assert box["y"] + box["height"] <= viewport["y"] + 1, f"{selector} remained visible past its block"


async def _open_debug_history(page: Page) -> None:
    await page.get_by_role("button", name="More", exact=True).click()
    await page.get_by_role("menuitem", name="Debug history").click()
    await expect(page.locator('[aria-label="Chronological observations"]')).to_be_visible()
    await expect(
        page.locator('[aria-label="Chronological observations"] [data-debug-observation]').first
    ).to_be_visible()
    # The opening menu item overlaps the drawer's pagination buttons.
    await page.mouse.move(0, 0)


async def _assert_phone_composer_layout(page: Page) -> None:
    send = page.locator('.agentplane-composer-send button[aria-label="Send"]')
    model = page.locator(".agentplane-composer-model")
    effort = page.locator(".agentplane-composer-effort")
    await expect(page.locator(".agentplane-topbar-title .agentplane-thread-status-indicator")).to_be_visible()
    await expect(page.locator(".agentplane-composer-controls")).to_be_visible()
    send_box = await send.bounding_box()
    model_box = await model.bounding_box()
    effort_box = await effort.bounding_box()
    viewport = page.viewport_size
    assert send_box is not None
    assert model_box is not None
    assert effort_box is not None
    assert viewport is not None
    assert send_box["width"] > 0
    assert send_box["x"] >= 0
    assert send_box["x"] + send_box["width"] <= viewport["width"]
    assert model_box["width"] > 0
    assert effort_box["width"] > 0
    assert abs(model_box["y"] - send_box["y"]) < 8
    assert abs(effort_box["y"] - send_box["y"]) < 8


async def _open_tool_run(page: Page) -> None:
    await _open_run(page)
    await _expand_tool_steps(page)


async def _expand_tool_steps(page: Page) -> None:
    steps = page.locator(".agentplane-step-details")
    await expect(steps.first).to_be_visible()
    # Wait for the opened run's payloads to render before counting its tool lines. A count sampled
    # while an OptionalPayload is loading can miss a later call and photograph a folded output.
    await expect(steps.locator('[aria-busy="true"]')).to_have_count(0)
    await expect(page.locator("[aria-label='Thread history'][data-layout-settled='true']")).to_be_attached()
    # Open each tool-call line once; leave reasoning folded.
    tool_steps = steps.filter(has_not=page.locator(".agentplane-step-title:text-is('Reasoning')"))
    count = await tool_steps.count()
    assert count > 0, "tool run contained no call lines"
    for index in range(count):
        control = tool_steps.nth(index).locator(".agentplane-disclosure-summary").first
        if await control.get_attribute("aria-expanded") == "false":
            await control.click()
    await expect(page.locator(".agentplane-step-details .agentplane-code-block .cm-content").first).to_be_attached()
    await wait_for_stable(page)


async def _open_select(
    page: Page, *, label: str, available: str, picked: str | None = None, press_arrow_down: bool = False
) -> None:
    selector = page.get_by_role("combobox", name=label, exact=True)
    await expect(selector).to_be_enabled()
    if press_arrow_down:
        await selector.press("ArrowDown")
    else:
        # Mantine opens this read-only combobox from its visible PillsInput wrapper.
        wrapper = selector.locator(
            "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' mantine-MultiSelect-input ')][1]"
        )
        await wrapper.click()
    await expect(page.get_by_role("option", name=re.compile(re.escape(available)))).to_be_visible()
    if picked is not None:
        await expect(page.get_by_role("option", name=re.compile(re.escape(picked)))).to_have_count(0)
    await page.mouse.move(0, 0)
    await wait_for_stable(page)
