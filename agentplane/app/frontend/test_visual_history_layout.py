"""Finished command history layout and interaction in the production frontend's fixture app.

Replication, cursor/replay, reconnect, delayed chunks, streaming and admission recovery remain
in test_thread_browser.py; these finished histories exercise Chromium layout and reader gestures.
"""

from typing import Literal

import pytest
import pytest_bazel
from playwright.async_api import Locator, expect

from agentplane.app.frontend.testing.app import IDLE_THREAD, AgentplaneFixture, Scene
from agentplane.app.frontend.testing.history_interactions import frames, holding_still, read_at, tool_call_in, wheel
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("color_scheme", ["light", "dark"], ids=["light", "dark"])
@pytest.mark.parametrize(
    ("viewport", "viewport_name"), [(DESKTOP, "desktop"), (MOBILE, "phone")], ids=["desktop", "phone"]
)
async def test_expanded_command_stays_collapsible(
    view: VisualPage,
    app: AgentplaneFixture,
    viewport: Viewport,
    viewport_name: str,
    color_scheme: Literal["light", "dark"],
) -> None:
    page = view.page
    color_suffix = "" if color_scheme == "light" else f"-{color_scheme}"
    await app.build(Scene.COLLAPSIBLE_COMMAND_HISTORY)
    await app.mount_thread(IDLE_THREAD)
    await expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", color_scheme)
    history = page.get_by_role("region", name="Thread history", exact=True)
    # Visit the preceding content before composing this review scene. Rows that left the
    # virtual window while their text loaded retain estimates until they are visited again;
    # which responses arrived first otherwise changes the run's fractional content offset.
    # Wheel through the history, preserving the real virtualizer and its loading behavior.
    for _ in range(40):
        await expect(history).to_have_attribute("data-layout-settled", "true", timeout=30_000)
        if await history.evaluate("area => area.scrollTop") == 0:
            break
        await wheel(page, -400)
    else:
        raise AssertionError("the review scene never reached the start of the history")
    await wheel(page, 10_000)
    await expect(history).to_have_attribute("data-layout-settled", "true", timeout=30_000)
    run = history.locator("[data-thread-anchor]").filter(has_text="3 tool calls")
    run_card = run.locator(":scope > .ui-collapsible-card")
    await read_at(page, run, 0.1)
    await run.locator(".ui-disclosure-summary").first.click()
    call, _ = tool_call_in(run, "a")
    await call.locator(".ui-disclosure-summary").first.click()
    command = call.locator('.ui-clamped-block[data-label="Command"]')
    output = call.locator('.ui-clamped-block[data-label="Output"]')
    show_all = command.get_by_role("button", name="Show all 24 lines", exact=True)
    collapse = command.get_by_role("button", name="Command", exact=True)
    await read_at(page, show_all, 0.3)
    editor = await command.locator(".cm-editor").element_handle()
    assert editor is not None

    async def select_command_line(line_text: str) -> None:
        line = command.locator(".cm-line").filter(has_text=line_text)
        await expect(line).to_have_count(1)
        await line.evaluate(
            """line => {
                const range = document.createRange();
                range.selectNodeContents(line);
                const selection = window.getSelection();
                if (!selection) throw new Error("the document has no text selection");
                selection.removeAllRanges();
                selection.addRange(range);
            }"""
        )
        assert await page.evaluate("() => window.getSelection()?.toString()") == line_text

    async def first_character_left(block: Locator) -> float:
        line = block.locator(".cm-line").first
        await expect(line).to_be_visible()
        return float(
            await line.evaluate(
                """line => {
                const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
                let text = walker.nextNode();
                while (text && !text.textContent) text = walker.nextNode();
                if (!text) throw new Error('code block has no text to measure');
                const range = document.createRange();
                range.setStart(text, 0);
                range.setEnd(text, 1);
                return range.getBoundingClientRect().left;
            }"""
            )
        )

    async def summary_label_left(heading: Locator) -> float:
        label = heading.locator(".ui-disclosure-summary-content")
        await expect(label).to_be_visible()
        bounds = await label.bounding_box()
        assert bounds is not None
        return bounds["x"]

    for repeat in range(2):
        await select_command_line("echo command line 0")
        command_text_before = await first_character_left(command)
        output_text_before = await first_character_left(output)
        await show_all.click()
        # Deliver layout/ResizeObserver callbacks: the old observer measured a detached node and
        # removed the collapse control after the first render of the expanded block.
        await frames(page)
        await expect(command).to_have_attribute("data-expanded", "true")
        await expect(command.locator(".ui-clamped-disclosure .ui-disclosure-heading button")).to_have_count(1)
        assert await editor.evaluate("node => node.isConnected"), "expanding remounted the CodeMirror editor"
        assert await page.evaluate("() => window.getSelection()?.toString()") == "echo command line 0"
        command_text_after = await first_character_left(command)
        output_text_after = await first_character_left(output)
        assert abs(command_text_after - command_text_before) <= 1, (command_text_before, command_text_after)
        assert abs(output_text_after - output_text_before) <= 1, (output_text_before, output_text_after)

        command_heading = command.locator(".ui-disclosure-heading").first
        output_heading = call.locator(".agentplane-output-disclosure .ui-disclosure-heading").first
        call_heading = call.locator(":scope > .ui-disclosure-item > .ui-disclosure-heading")
        command_label_left = await summary_label_left(command_heading)
        output_label_left = await summary_label_left(output_heading)
        call_label_left = await summary_label_left(call_heading)
        assert abs(command_text_after - command_label_left) <= 1, (command_text_after, command_label_left)
        assert abs(output_text_after - output_label_left) <= 1, (output_text_after, output_label_left)
        command_inset = command_text_after - call_label_left
        output_inset = output_text_after - call_label_left
        assert abs(command_inset - output_inset) <= 1, (command_inset, output_inset)
        await read_at(page, command.locator(".cm-line").filter(has_text="echo command line 20"), 0.5)
        await expect(collapse).to_be_in_viewport()
        await select_command_line("echo command line 20")
        if repeat == 0:  # review outputs are unique per name
            await view.capture(
                f"thread-command-expanded-{viewport_name}{color_suffix}",
                label=f"Expanded command on {viewport_name} ({color_scheme})",
            )
        # The heading the reader clicked hides with the block it collapses: the history leaves the run where it was.
        async with holding_still(page, run):
            await collapse.click()
        await frames(page)
        assert await editor.evaluate("node => node.isConnected"), "collapsing remounted the CodeMirror editor"
        assert await page.evaluate("() => window.getSelection()?.toString()") == "echo command line 20"
        await expect(command.locator('[data-clamped="true"]')).to_have_count(1)
        await expect(show_all).to_be_visible()
        await expect(collapse).to_have_count(0)

        run_heading = run_card.locator(":scope > .ui-disclosure > .ui-disclosure-item > .ui-disclosure-heading")
        call_heading = call.locator(":scope > .ui-disclosure-item > .ui-disclosure-heading")
        output_heading = call.locator(".agentplane-output-disclosure > .ui-disclosure-item > .ui-disclosure-heading")
        headings = [run_heading, call_heading, output_heading]
        heading_boxes = [await heading.bounding_box() for heading in headings]
        frame_box = await run_card.bounding_box()
        assert all(box is not None for box in heading_boxes), heading_boxes
        assert frame_box is not None
        boxes = [box for box in heading_boxes if box is not None]

        # The row headings own the separators: nested rows fill the shared card frame even though
        # their labels and terminal content indent independently.
        assert abs(boxes[0]["x"] - (frame_box["x"] + 1)) <= 1, (boxes[0], frame_box)
        assert abs(boxes[0]["x"] + boxes[0]["width"] - (frame_box["x"] + frame_box["width"] - 1)) <= 1, (
            boxes[0],
            frame_box,
        )
        left_edges = [box["x"] for box in boxes]
        right_edges = [box["x"] + box["width"] for box in boxes]
        assert max(left_edges) - min(left_edges) <= 1, boxes
        assert max(right_edges) - min(right_edges) <= 1, boxes
        assert abs(boxes[1]["y"] - (boxes[0]["y"] + boxes[0]["height"])) <= 1, boxes

        expected_header_height = 44 if viewport.width < 768 else 28
        assert max(box["height"] for box in boxes) - min(box["height"] for box in boxes) <= 1, boxes
        for heading, heading_box in zip(headings, boxes, strict=True):
            assert abs(heading_box["height"] - expected_header_height) <= 1, (heading_box, viewport)
            label_box = await heading.locator(".ui-disclosure-summary-content").bounding_box()
            control_box = await heading.locator(".ui-disclosure-summary").bounding_box()
            assert label_box is not None
            assert control_box is not None
            label_center = label_box["y"] + label_box["height"] / 2
            heading_center = heading_box["y"] + heading_box["height"] / 2
            assert abs(label_center - heading_center) <= 1, (label_box, heading_box)
            assert abs(control_box["height"] - heading_box["height"]) <= 1, (control_box, heading_box)
            text_box = await heading.locator(".mantine-Text-root").first.bounding_box()
            assert text_box is not None
            text_center = text_box["y"] + text_box["height"] / 2
            assert abs(text_center - heading_center) <= 1, (text_box, heading_box)

        preview_box = await call_heading.locator(".transcript-step-preview").bounding_box()
        assert preview_box is not None
        call_center = boxes[1]["y"] + boxes[1]["height"] / 2
        preview_center = preview_box["y"] + preview_box["height"] / 2
        assert abs(preview_center - call_center) <= 1, (preview_box, boxes[1])

        raw_root = call.locator(".transcript-step-controls .mantine-Switch-root")
        raw_box = await raw_root.bounding_box()
        assert raw_box is not None
        call_right = boxes[1]["x"] + boxes[1]["width"]
        assert call_right - (raw_box["x"] + raw_box["width"]) >= 8, (raw_box, boxes[1])

        command_editor = command.locator(".cm-editor").first
        await expect(command_editor).to_be_visible()
        editor_box = await command_editor.bounding_box()
        assert editor_box is not None
        editor_borders = await command_editor.evaluate(
            """element => {
                const style = getComputedStyle(element);
                return [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth];
            }"""
        )
        assert editor_borders == ["0px", "0px", "0px", "0px"], editor_borders
        frame_right = boxes[0]["x"] + boxes[0]["width"]
        assert frame_right - (editor_box["x"] + editor_box["width"]) >= 8, (editor_box, boxes[0])

        nested_heading_display = await command.evaluate(
            """block => {
                const panel = block.querySelector(
                    ".ui-clamped-disclosure .ui-disclosure-content"
                );
                if (!panel) throw new Error("clamped disclosure panel is missing");
                const nested = document.createElement("div");
                nested.className = "ui-disclosure";
                const item = document.createElement("div");
                item.className = "ui-disclosure-item";
                const heading = document.createElement("div");
                heading.className = "ui-disclosure-header ui-disclosure-heading";
                item.append(heading);
                nested.append(item);
                panel.append(nested);
                const display = getComputedStyle(heading).display;
                nested.remove();
                return display;
            }"""
        )
        assert nested_heading_display != "none", "collapsed ClampedBlock styles hid a nested disclosure heading"
        await expect(call.locator(".ui-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
        if repeat == 0:
            await view.capture(
                f"thread-command-collapsed-{viewport_name}{color_suffix}",
                label=f"Collapsed command on {viewport_name} ({color_scheme})",
            )


@pytest.mark.parametrize("following", [False, True], ids=["mid-thread", "following"])
@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "phone"])
async def test_opening_a_call_and_its_output_keeps_it_collapsible_while_reading(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport, following: bool
) -> None:
    """Opening a run or call preserves its row; a long output stays collapsible while the reader scrolls it."""
    page = view.page
    await app.build(Scene.COLLAPSIBLE_COMMAND_HISTORY if following else Scene.MID_THREAD_COMMAND_HISTORY)
    await app.mount_thread(IDLE_THREAD)
    await expect(page.get_by_role("region", name="Thread history", exact=True)).to_have_attribute(
        "data-layout-settled", "true"
    )
    history = page.get_by_role("region", name="Thread history", exact=True)
    run = history.locator("[data-thread-anchor]").filter(has_text="3 tool calls")
    if following:
        await page.wait_for_function(
            """() => {
                const area = document.querySelector('[aria-label="Thread history"]');
                return area.scrollHeight - area.clientHeight - area.scrollTop <= 2;
            }"""
        )
    else:
        await read_at(page, run, 0.1)

    # Expanding a row preserves its place in the reader's viewport.
    summary = run.locator(".ui-disclosure-summary").first
    async with holding_still(page, run):
        await summary.click()
        await expect(run.locator(".transcript-step-details")).to_have_count(3)

    call, card = tool_call_in(run, "a")
    show_all = call.get_by_role("button", name="Show all 60 lines")
    async with holding_still(page, card):
        await call.locator(".ui-disclosure-summary").first.click()
        await expect(show_all).to_be_visible()
    await view.capture(f"{view.capture_name}-call-open")

    await read_at(page, show_all, 0.3)
    await show_all.click()
    collapse = call.locator(".agentplane-output-disclosure .ui-disclosure-summary")
    output_line = call.locator('.ui-clamped-block[data-label="Output"] .cm-line').filter(has_text="output line 45")
    await wheel(page, 500)
    await expect(output_line).to_be_visible()
    await read_at(page, output_line, 0.5)
    await expect(collapse).to_be_in_viewport()
    await expect(collapse).to_have_attribute("aria-expanded", "true")
    collapse_box = await collapse.bounding_box()
    output_heading = call.locator(".agentplane-output-disclosure .ui-disclosure-heading")
    output_heading_box = await output_heading.bounding_box()
    call_heading_box = await call.locator(".ui-disclosure-heading").first.bounding_box()
    run_heading_box = await run.locator(".ui-disclosure-heading").first.bounding_box()
    history_box = await history.bounding_box()
    assert collapse_box is not None
    assert output_heading_box is not None
    assert call_heading_box is not None
    assert run_heading_box is not None
    assert history_box is not None
    minimum_control_height = 44 if viewport == MOBILE else 28
    assert collapse_box["height"] >= minimum_control_height, f"collapse target is too short: {collapse_box}"
    if viewport == DESKTOP:
        for heading_name, heading_box in (
            ("run", run_heading_box),
            ("tool call", call_heading_box),
            ("output", output_heading_box),
        ):
            assert heading_box["height"] <= 30, f"desktop {heading_name} heading is too tall: {heading_box}"
    card_box = await card.bounding_box()
    assert card_box is not None
    assert output_heading_box["x"] >= card_box["x"] - 1
    assert output_heading_box["x"] + output_heading_box["width"] <= card_box["x"] + card_box["width"] + 1
    assert abs(run_heading_box["y"] - history_box["y"]) <= 2, (
        f"the run heading should use the history's top sticky slot: {run_heading_box=} {history_box=}"
    )
    assert abs(call_heading_box["y"] - (run_heading_box["y"] + run_heading_box["height"])) <= 2, (
        f"the tool-call heading should stack below the run heading: {call_heading_box=} {run_heading_box=}"
    )
    assert abs(output_heading_box["y"] - (call_heading_box["y"] + call_heading_box["height"])) <= 2, (
        f"the output heading should stack below the tool-call heading: {output_heading_box=} {call_heading_box=}"
    )
    assert abs(collapse_box["y"] - output_heading_box["y"]) <= 2, (
        f"the output accordion control should occupy its sticky heading row: {collapse_box=} {output_heading_box=}"
    )
    active_sticky_action = await page.evaluate(
        """point => document.elementFromPoint(point.x, point.y)?.closest('button')?.getAttribute('aria-expanded')""",
        {"x": collapse_box["x"] + collapse_box["width"] / 2, "y": collapse_box["y"] + collapse_box["height"] / 2},
    )
    assert active_sticky_action == "true", (
        f"a parent heading obscured the output Disclosure control: {active_sticky_action}"
    )
    show_less = output_heading.get_by_role("button", name="Show less Output")
    await expect(show_less).to_be_in_viewport(ratio=1)
    await show_less.click()
    await expect(call.locator('.ui-clamped-block[data-label="Output"]')).to_have_attribute("data-expanded", "false")
    await expect(output_line).not_to_be_in_viewport()
    await expect(show_all).to_be_visible()
    await show_all.click()
    await read_at(page, output_line, 0.5)
    await expect(collapse).to_be_in_viewport()

    # At the true tail, collapsing a long block can shorten the history below its current scrollTop.
    # The browser must clamp to the new bottom; pixel-stable anchoring is asserted mid-thread, where
    # the history has room to preserve the clicked heading's position.
    if following:
        await collapse.click()
    else:
        async with holding_still(page, collapse):
            await collapse.click()
    await expect(history).to_have_attribute("data-scroll-mode", "reading")
    await expect(collapse).to_have_attribute("aria-expanded", "false")
    await expect(output_line).to_be_hidden()
    await expect(call.locator(".ui-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
    await expect(run.locator(".ui-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
    for heading in (
        run.locator(".ui-disclosure-heading").first,
        call.locator(".ui-disclosure-heading").first,
        call.locator(".agentplane-output-disclosure .ui-disclosure-heading"),
    ):
        await expect(heading).to_be_in_viewport()
    await view.capture(f"{view.capture_name}-output-collapsed")


if __name__ == "__main__":
    pytest_bazel.main()
