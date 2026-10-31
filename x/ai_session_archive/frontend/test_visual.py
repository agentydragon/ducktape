"""Visual check for the local workstation session browser."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_session_list_and_compact_import_progress(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "session-list-and-compact-import-progress",
        viewport=Viewport(width=1440, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
        progress = page.get_by_role("region", name="Import progress by machine")
        await expect(progress).to_contain_text("ThinkPad")
        await expect(progress).to_contain_text("Atlas")
        await expect(progress).to_contain_text("Rugged")
        await expect(progress).to_contain_text("ThinkPad")
        await expect(progress).to_contain_text("72/198 snapshots remaining")
        await expect(progress).to_contain_text("Complete fixture machine")
        await expect(progress.get_by_role("status", name="Complete fixture machine synced")).to_be_visible()
        await expect(progress).not_to_contain_text("Snapshots: 3 of 5 registered complete")
        await expect(page.get_by_role("progressbar", name="Complete fixture machine snapshot progress")).to_have_count(
            0
        )
        rugged_progress = page.get_by_role("progressbar", name="Rugged snapshot progress")
        await expect(rugged_progress).to_have_attribute("aria-valuenow", "126")
        await expect(rugged_progress).to_have_attribute("aria-valuemax", "198")
        await expect(rugged_progress).to_have_attribute(
            "aria-valuetext", "126 of 198 registered snapshots complete, 0 failed"
        )
        heading = await page.get_by_role("heading", name="Local agent sessions").bounding_box()
        import_bounds = await progress.bounding_box()
        assert heading is not None
        assert import_bounds is not None
        assert import_bounds["x"] > heading["x"] + heading["width"]
        assert abs(import_bounds["y"] - heading["y"]) < 30
        await expect(page.get_by_text("ThinkPad", exact=True).first).to_be_visible()
        await expect(page.get_by_role("heading", name="Compare two captured versions")).to_be_visible()
        await expect(page.locator('[aria-label="Session transcript"]')).to_contain_text(
            "I found two snapshots for this session."
        )
        options = page.get_by_role("button", name="Conversation options", exact=True)
        await expect(options).to_be_visible()
        await expect(page.locator('select[aria-label="Captured version"]')).to_have_count(0)
        await view.check(context="machine list and latest capture are visible with compact conversation controls")
        await view.capture(target=page.locator("#root"))
        await options.click()
        await expect(page.get_by_role("menu")).to_have_css("opacity", "1")
        await expect(page.locator('[role="menuitemradio"][data-version-id="codex-version-2"]')).to_have_attribute(
            "aria-checked", "true"
        )
        await expect(page.get_by_role("menuitemradio", name=re.compile("Latest captured"))).to_be_visible()
        await view.capture(name=f"{capture_name}_conversation_options")
        await page.keyboard.press("Escape")


async def test_import_progress_details(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "import-progress-details",
        viewport=Viewport(width=1440, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await page.get_by_role("link", name="Import details").click()
        await expect(page.get_by_role("heading", name="Import progress details")).to_be_visible()
        details = page.get_by_role("region", name="Import progress by machine")
        await expect(details).to_contain_text("Snapshots: 3 of 5 registered complete")
        await expect(details).to_contain_text("Codex metadata: 0 pending · 0 failed · 1 partial")
        await expect(details).to_contain_text("Complete fixture machine")
        await expect(details).to_contain_text("File counts cover entries inventoried so far")
        await view.check(context="full progress breakdown is visible for every registered machine")
        await view.capture(target=page.locator("#root"))

        await page.get_by_role("link", name="Back to sessions").click()
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()


async def test_compact_import_progress_on_mobile(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "compact-import-progress-mobile",
        viewport=Viewport(width=390, height=844),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        progress = page.get_by_role("region", name="Import progress by machine")
        thinkpad_progress = page.get_by_role("progressbar", name="ThinkPad file-entry progress")
        session_list_toggle = page.get_by_role("button", name="Session list")
        await expect(progress).to_contain_text("27/31")
        await expect(progress).to_contain_text("Complete fixture machine")
        await expect(progress.get_by_role("status", name="Complete fixture machine synced")).to_be_visible()
        await expect(thinkpad_progress).to_have_attribute("aria-valuemax", "31")
        await expect(session_list_toggle).to_be_visible()
        await expect(progress.get_by_role("link", name="Import details")).to_be_visible()

        progress_bounds = await thinkpad_progress.bounding_box()
        assert progress_bounds is not None
        assert progress_bounds["x"] >= 0
        assert progress_bounds["x"] + progress_bounds["width"] <= 390
        assert progress_bounds["y"] >= 0
        assert progress_bounds["y"] + progress_bounds["height"] <= 844

        await view.check(context="compact import progress and the session-list button fit the mobile viewport")
        await view.capture(target=page.locator("#root"))

        await page.get_by_role("button", name="Conversation options", exact=True).click()
        menu = page.get_by_role("menu")
        await expect(menu).to_be_visible()
        await expect(menu).to_have_css("opacity", "1")
        menu_bounds = await menu.bounding_box()
        assert menu_bounds is not None
        assert menu_bounds["x"] >= 0
        assert menu_bounds["x"] + menu_bounds["width"] <= 390
        await view.capture(name=f"{capture_name}_conversation_options")
        await page.keyboard.press("Escape")

        await session_list_toggle.click()
        sidebar = page.get_by_role("dialog", name="Session list")
        session_list_refresh = sidebar.get_by_role("button", name="Refresh session list")
        await expect(sidebar).to_be_visible()
        await expect(session_list_refresh).to_be_visible()
        sidebar_bounds = await sidebar.bounding_box()
        refresh_bounds = await session_list_refresh.bounding_box()
        assert sidebar_bounds is not None
        assert refresh_bounds is not None
        assert sidebar_bounds["y"] >= 0
        assert sidebar_bounds["y"] + sidebar_bounds["height"] <= 844
        assert refresh_bounds["y"] + refresh_bounds["height"] <= sidebar_bounds["y"] + sidebar_bounds["height"]


async def test_inferred_subagent_parent_link(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "inferred-subagent-parent-link",
        viewport=Viewport(width=1440, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await page.get_by_role("checkbox", name="Subagents", exact=True).click()
        await expect(page.get_by_role("checkbox", name="Subagents", exact=True)).to_be_checked()
        await page.get_by_role("link", name="Inspect a local transcript").click()
        related_toggle = page.get_by_role("button", name=re.compile(r"^Related sessions \("))
        await related_toggle.click()
        related = page.get_by_role("region", name="Related sessions")
        await expect(related).to_contain_text("Parent inferred from Claude subagent path")
        await expect(related).to_contain_text("Inferred Claude parent · session 12ab")
        await expect(page.get_by_role("button", name="Conversation options", exact=True)).to_be_visible()
        await view.check(context="child transcript and inferred parent link are visible")
        await view.capture(target=page.locator("body"))


@pytest.mark.parametrize(
    "viewport", [Viewport(width=1440, height=1000), Viewport(width=390, height=844)], ids=["desktop", "mobile"]
)
async def test_related_sessions_menu_stays_with_title(
    visual: VisualHarness, capture_name: str, viewport: Viewport
) -> None:
    async with visual.open(
        "long-child-list", viewport=viewport, color_scheme="light", capture_name=capture_name
    ) as view:
        page = view.page
        if viewport.width <= 760:
            await page.get_by_role("button", name="Session list").click()
        await page.get_by_role("checkbox", name="Subagents", exact=True).click()
        await expect(page.get_by_role("checkbox", name="Subagents", exact=True)).to_be_checked()
        await page.get_by_role("link", name="Inspect a local transcript").click()
        toggle = page.locator(".session-browser-related-trigger")
        await expect(toggle).to_have_attribute("aria-label", "Related sessions (51 shown, more available)")
        await expect(toggle.locator(".session-browser-related-count")).to_have_text("51+")
        await expect(toggle).to_be_visible()
        await toggle.click()

        related = page.get_by_role("region", name="Related sessions")
        dropdown = page.locator(".session-browser-related-dropdown")
        await expect(related).to_contain_text("Inferred Claude parent · session 12ab")
        await expect(related).to_contain_text("Inspect module 01")
        dropdown_bounds = await dropdown.bounding_box()
        assert dropdown_bounds is not None
        assert dropdown_bounds["x"] >= 0
        assert dropdown_bounds["x"] + dropdown_bounds["width"] <= viewport.width
        assert dropdown_bounds["y"] >= 0
        assert dropdown_bounds["y"] + dropdown_bounds["height"] <= viewport.height

        await view.check(context="related-session count and links remain visible beside the selected title")
        await view.capture(target=page.locator("body"))


@pytest.mark.parametrize(
    "viewport", [Viewport(width=1440, height=780), Viewport(width=390, height=844)], ids=["desktop", "mobile"]
)
async def test_long_child_list_stays_scrollable_without_covering_transcript(
    visual: VisualHarness, capture_name: str, viewport: Viewport
) -> None:
    async with visual.open(
        "long-child-list", viewport=viewport, color_scheme="light", capture_name=capture_name
    ) as view:
        page = view.page
        if viewport.width <= 760:
            await page.get_by_role("button", name="Session list").click()
        await page.get_by_role("checkbox", name="Subagents", exact=True).click()
        await expect(page.get_by_role("checkbox", name="Subagents", exact=True)).to_be_checked()
        await page.get_by_role("link", name="Inspect a local transcript").click()
        related_toggle = page.locator(".session-browser-related-trigger")
        await expect(related_toggle).to_have_attribute("aria-label", "Related sessions (51 shown, more available)")
        await expect(related_toggle.locator(".session-browser-related-count")).to_have_text("51+")
        await related_toggle.click()

        related = page.get_by_role("region", name="Related sessions")
        children = page.get_by_role("region", name="Child sessions")
        transcript = page.get_by_role("region", name="Session transcript")
        await expect(related).to_contain_text("Inferred Claude parent · session 12ab")
        await expect(children).to_contain_text("Inspect module 01")
        await expect(transcript).to_contain_text("The capture preserves the original JSONL records.")
        assert await children.evaluate("element => element.scrollHeight > element.clientHeight")

        await related.get_by_role("button", name="Load more child sessions").click()
        await expect(children).to_contain_text("Inspect module 63")
        await expect(related_toggle).to_have_attribute("aria-label", "Related sessions (64 shown)")
        await related.evaluate(
            "element => { element.scrollIntoView({ block: 'nearest' }); element.scrollTop = element.scrollHeight; }"
        )
        await children.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        last_child = children.get_by_role("link", name="Inspect module 63")
        last_child_visibility = await last_child.evaluate(
            """node => {
                const range = document.createRange();
                range.selectNodeContents(node);
                const textRect = range.getBoundingClientRect();
                const clippingBounds = [{ top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth }];
                for (let ancestor = node.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
                  if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(ancestor).overflowY)) continue;
                  const rect = ancestor.getBoundingClientRect();
                  clippingBounds.push({
                    top: rect.top + ancestor.clientTop,
                    bottom: rect.top + ancestor.clientTop + ancestor.clientHeight,
                    left: rect.left + ancestor.clientLeft,
                    right: rect.left + ancestor.clientLeft + ancestor.clientWidth,
                  });
                }
                return {
                  visible: clippingBounds.every(bounds => textRect.top >= bounds.top && textRect.bottom <= bounds.bottom &&
                    textRect.left >= bounds.left && textRect.right <= bounds.right),
                  text: { top: textRect.top, bottom: textRect.bottom, left: textRect.left, right: textRect.right },
                  clippingBounds,
                };
            }"""
        )
        assert last_child_visibility["visible"], (
            "the final child link text must fit in the viewport and every enclosing scrollport: "
            f"{last_child_visibility}"
        )
        await expect(transcript).to_contain_text("The capture preserves the original JSONL records.")
        await related_toggle.click()

        events = page.get_by_role("region", name="Conversation records")
        geometry = await events.evaluate(
            "element => { const rect = element.getBoundingClientRect(); return { height: element.clientHeight, top: rect.top, bottom: rect.bottom, viewport: window.innerHeight }; }"
        )
        transcript_viewport_is_usable = (
            geometry["height"] >= 150 and geometry["top"] >= 0 and geometry["bottom"] <= geometry["viewport"]
        )
        assert transcript_viewport_is_usable, f"conversation viewport is too small or outside the viewport: {geometry}"
        await expect(events).to_have_attribute("data-layout-settled", "true")
        assert await events.evaluate(
            "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 1"
        )
        await view.check(context="child sessions scroll independently while the conversation remains available")
        await view.capture(target=page.locator("#root"), name=f"{capture_name}_child_list_end")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_shared_transcript_disclosures(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "shared-transcript-disclosures",
        viewport=Viewport(width=width, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        await page.get_by_role("link", name=re.compile("Tools and reasoning fixture")).click()
        history = page.get_by_role("region", name="Conversation records")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        group = history.get_by_role("button", name=re.compile("read_build_log"))
        await expect(group).to_have_attribute("aria-expanded", "false")
        await expect(history).to_contain_text("missing test configuration")
        await expect(history.get_by_text("I will inspect the build log.", exact=True)).to_be_visible()
        await view.capture(name=f"{capture_name}_folded")
        user = history.locator('[data-event-ordinal="2"]')
        raw = user.get_by_role("button", name="Raw JSONL record", exact=True)
        assert await raw.evaluate("element => getComputedStyle(element).position === 'absolute'")
        await expect(user.get_by_role("region", name="Raw JSONL record")).to_have_count(0)
        await user.locator(".transcript-user-bubble").hover()
        await expect(raw).to_be_visible()
        await raw.click()
        raw_panel = user.get_by_role("region", name="Raw JSONL record", exact=True)
        await expect(raw_panel).to_contain_text("Inspect the archived build failure")
        await raw_panel.get_by_role("button", name="Exact line bytes (base64)").click()
        await expect(raw_panel.locator("pre")).to_have_count(2)
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await view.capture(name=f"{capture_name}_raw_inspection")
        await raw.click()
        await expect(raw_panel).to_have_count(0)
        await group.click()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(group).to_have_attribute("aria-expanded", "true")
        await expect(history.get_by_text("I will inspect the build log.", exact=True)).to_be_visible()
        tool = history.locator('[data-activity-step="tool"]')
        await expect(tool).to_contain_text("Fixture build output")
        await tool.scroll_into_view_if_needed()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(tool).to_be_in_viewport()
        await view.capture(name=f"{capture_name}_expanded")
        await group.click()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(group).to_have_attribute("aria-expanded", "false")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_codex_work_steps(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "codex-work-steps", viewport=Viewport(width=width, height=1000), color_scheme="light", capture_name=capture_name
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        await page.get_by_role("link", name=re.compile("Archive transcript UI work")).click()
        history = page.get_by_role("region", name="Conversation records")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(history).to_contain_text("The components are shared.")
        await expect(history).to_contain_text("Context compacted")
        await expect(history).not_to_contain_text("Structured record;")
        await expect(history.locator("[data-activity-step]")).to_have_count(0)
        await view.capture(name=f"{capture_name}_collapsed")

        await history.get_by_role("button", name="1 tool call, 1 agent message", exact=True).click()
        await history.get_by_role("button", name="1 tool call, 2 reasoning steps", exact=True).click()
        await expect(history.locator('[data-activity-step="tool"]')).to_have_count(2)
        await expect(history.locator('[data-activity-step="reasoning"]')).to_have_count(1)
        await expect(history.locator('[data-activity-step="subagent"]')).to_contain_text("/root/shared_steps")
        await view.capture(name=f"{capture_name}_steps")

        tool = history.locator('[data-activity-step="tool"]').filter(has_text="run_checks")
        await tool.get_by_role("button", name=re.compile("run_checks")).click()
        await tool.get_by_role("region", name="Output", exact=True).scroll_into_view_if_needed()
        await expect(tool).to_contain_text("PASS check 01")
        await expect(tool.locator('[data-clamped="true"]')).to_have_count(1)
        await tool.scroll_into_view_if_needed()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await view.capture(name=f"{capture_name}_tool_output")
        await tool.get_by_role("button", name="Show all 48 lines", exact=True).click()
        await expect(tool.locator('[data-clamped="true"]')).to_have_count(0)
        await tool.hover()
        await tool.get_by_role("button", name="Raw JSONL record", exact=True).click()
        await expect(tool.get_by_role("region", name="Raw JSONL record")).to_have_count(2)
        await expect(tool).to_contain_text('"call_id":"check"')
        await tool.get_by_role("region", name="Raw JSONL record").first.scroll_into_view_if_needed()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await view.capture(name=f"{capture_name}_source")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_claude_attachment_widgets(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "claude-attachment-widgets",
        viewport=Viewport(width=width, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        await page.get_by_role("link", name=re.compile("Claude attachment disclosures")).click()
        history = page.get_by_role("region", name="Conversation records")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await history.get_by_role("button", name="2 attachments", exact=True).click()
        attachments = history.locator('[data-activity-step="attachment"]')
        await expect(attachments).to_have_count(2)
        await expect(history).not_to_contain_text("Structured record;")
        await expect(history).not_to_contain_text("export const enabled")
        await view.capture(name=f"{capture_name}_collapsed")
        edited = attachments.filter(has_text="edited text file")
        await edited.get_by_role("button", name=re.compile("Attachment · edited text file")).click()
        await edited.get_by_role("region", name="Details", exact=True).scroll_into_view_if_needed()
        await expect(edited).to_contain_text("export const enabled = true;")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await view.capture(name=f"{capture_name}_expanded")


if __name__ == "__main__":
    pytest_bazel.main()


@pytest.mark.parametrize(
    "viewport", [Viewport(width=1440, height=1000), Viewport(width=390, height=844)], ids=["desktop", "mobile"]
)
async def test_session_kind_filters(visual: VisualHarness, capture_name: str, viewport: Viewport) -> None:
    async with visual.open(
        "session-kind-filters", viewport=viewport, color_scheme="light", capture_name=capture_name
    ) as view:
        page = view.page
        if viewport.width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        sidebar = page.locator("#session-browser-sidebar")
        await expect(sidebar.get_by_role("checkbox", name="Main sessions", exact=True)).to_be_checked()
        await expect(sidebar.get_by_role("checkbox", name="Unknown", exact=True)).to_be_checked()
        await expect(sidebar.get_by_role("checkbox", name="Guardian", exact=True)).not_to_be_checked()
        await expect(sidebar.get_by_role("checkbox", name="Subagents", exact=True)).not_to_be_checked()
        await expect(sidebar.get_by_role("link", name="Compare two captured versions")).to_be_visible()
        await expect(sidebar.get_by_role("link", name="Guardian automation fixture")).to_have_count(0)
        await expect(sidebar.get_by_role("link", name="Inspect a local transcript")).to_have_count(0)
        await expect(sidebar.get_by_role("button", name="Refresh session list")).to_be_enabled()
        await view.check(context="default kinds keep the session list focused on main and unclassified sessions")
        await view.capture(target=page.locator("#root"))
        await sidebar.get_by_role("checkbox", name="Guardian", exact=True).click()
        await expect(sidebar.get_by_role("checkbox", name="Guardian", exact=True)).to_be_checked()
        await sidebar.get_by_role("checkbox", name="Subagents", exact=True).click()
        await expect(sidebar.get_by_role("checkbox", name="Subagents", exact=True)).to_be_checked()
        await expect(sidebar.get_by_role("link", name="Guardian automation fixture")).to_be_visible()
        await expect(sidebar.get_by_role("link", name="Inspect a local transcript")).to_be_visible()
        await expect(sidebar.get_by_role("button", name="Refresh session list")).to_be_enabled()
        await view.capture(name=f"{capture_name}_all_kinds", target=page.locator("#root"))
