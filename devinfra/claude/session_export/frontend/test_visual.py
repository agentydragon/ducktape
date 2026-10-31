"""Session viewer behavior and screenshot checkpoints driven by Playwright."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import Locator, Page, expect

from util.testing.page_capture import wait_for_stable
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness, VisualPage

pytest_plugins = ("util.testing.visual_fixtures",)


pytestmark = pytest.mark.asyncio(loop_scope="session")


_DESKTOP = Viewport()
_PHONE = Viewport(width=420, height=900)

_TRANSCRIPT = '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'


@asynccontextmanager
async def _fixture(
    visual: VisualHarness,
    fixture_id: str,
    *,
    viewport: Viewport,
    capture_name: str,
    color_scheme: Literal["light", "dark"] = "light",
) -> AsyncIterator[VisualPage]:
    async with visual.open(fixture_id, viewport=viewport, color_scheme=color_scheme, capture_name=capture_name) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.check(context="fixture ready")
        yield view


@pytest_asyncio.fixture(loop_scope="session")
async def noisy_view(visual: VisualHarness, viewport: Viewport, capture_name: str) -> AsyncIterator[VisualPage]:
    async with _fixture(visual, "noisy", viewport=viewport, capture_name=capture_name) as view:
        await _noisy_ready(view.page)
        yield view


@pytest_asyncio.fixture(loop_scope="session")
async def history_view(visual: VisualHarness, viewport: Viewport, capture_name: str) -> AsyncIterator[VisualPage]:
    async with _fixture(visual, "history", viewport=viewport, capture_name=capture_name) as view:
        yield view


@pytest_asyncio.fixture(loop_scope="session")
async def sidebar_view(visual: VisualHarness, viewport: Viewport, capture_name: str) -> AsyncIterator[VisualPage]:
    async with _fixture(visual, "sidebar", viewport=viewport, capture_name=capture_name) as view:
        await _noisy_ready(view.page)
        yield view


async def _capture(view: VisualPage) -> None:
    # Keep hover states out of layout checkpoints.
    await view.page.mouse.move(0, 0)
    await view.capture(target=view.page.locator("#app"))


async def _scroll_into_view(target: Locator) -> None:
    await target.evaluate(
        """element => {
            const viewport = document.querySelector('[aria-label="Session transcript"] .mantine-ScrollArea-viewport');
            if (viewport === null) throw new Error('Transcript viewport is missing');
            const targetTop = Math.max(0, (viewport.clientHeight - element.getBoundingClientRect().height) / 2);
            viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - targetTop;
            viewport.dispatchEvent(new Event('scroll'));
        }"""
    )


async def _click_right_edge(page: Page, button: Locator) -> None:
    await _scroll_into_view(button)
    await wait_for_stable(page)
    geometry = await button.evaluate(
        """button => {
            const rect = button.getBoundingClientRect();
            const parent = button.parentElement.getBoundingClientRect();
            const x = rect.right - 2, y = rect.top + rect.height / 2;
            return { width: rect.width, parentWidth: parent.width, height: rect.height, x, y,
                     clickable: button.contains(document.elementFromPoint(x, y)) };
        }"""
    )
    assert geometry["width"] >= geometry["parentWidth"] - 2, "Disclosure does not fill its row"
    assert geometry["height"] <= 28, "Disclosure exceeds 28px"
    assert geometry["clickable"], "Right edge of disclosure is not clickable"
    await page.mouse.click(geometry["x"], geometry["y"])


async def _noisy_ready(page: Page) -> None:
    await expect(page.locator('[data-message-role="assistant"]').first).to_be_attached()
    await expect(page.locator('[data-fold-kind="notice"]')).to_have_count(0)


async def _expect_compact_event_timeline(page: Page) -> Locator:
    strips = page.locator("[data-event-strip]")
    await expect(strips.first).to_be_attached()
    sequences = await strips.evaluate_all(
        "strips => strips.flatMap(strip => strip.dataset.historySequences.split(' '))"
    )
    assert len(sequences) == len(set(sequences)) == await page.evaluate("window.__visualFixture__.noisyEventCount")
    for strip in await strips.all():
        assert await strip.evaluate("element => element.getBoundingClientRect().height") <= 28
        assert await strip.locator("[data-event-dot]").count() <= 12
        assert await strip.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    await expect(page.locator("[data-event-json]")).to_have_count(0)
    assert await page.locator('[data-fold-kind="tool-run"]').count() <= 3
    return strips


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
async def test_completed_activity(visual: VisualHarness, viewport: Viewport, capture_name: str, expanded: bool) -> None:
    async with _fixture(visual, "completed-activity", viewport=viewport, capture_name=capture_name) as view:
        fixture = await view.page.evaluate("window.__visualFixture__")
        activity = view.page.locator('[data-fold-kind="activity"]').filter(
            has=view.page.locator("summary").filter(has_text=fixture["longCommandActivityTitle"])
        )
        await expect(activity).to_be_attached()
        assert await activity.evaluate("element => element.getBoundingClientRect().height") <= 28
        if expanded:
            await activity.locator("summary").click()
            await expect(activity).to_have_attribute("open", "")
            await expect(activity.locator("[data-activity-title]")).to_have_text(fixture["longCommandActivityTitle"])
            await expect(activity.locator("[data-activity-detail]")).to_have_text(fixture["longCommandActivityDetail"])
            await expect(activity.locator("[data-activity-title]")).to_be_visible()
            await expect(activity.locator("[data-activity-detail]")).to_be_visible()
        await _scroll_into_view(activity)
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_markdown_is_sanitized(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "markdown", viewport=viewport, capture_name=capture_name) as view:
        page = view.page
        for selector in (
            '[aria-label="Session history"]',
            '[data-message-role="user"] .ui-markdown ul',
            '[data-message-role="user"] .ui-markdown table',
            '[data-message-role="assistant"] .ui-markdown h2',
            '[data-message-role="assistant"] .ui-markdown a[href^="https://"]',
            '[data-message-role="assistant"] .ui-markdown .ui-code-block .cm-editor',
        ):
            await expect(page.locator(selector).first).to_be_attached()
        markdown = page.locator('[data-message-role="user"] .ui-markdown, [data-message-role="assistant"] .ui-markdown')
        await expect(markdown.locator("script, img, [onclick], [onerror]")).to_have_count(0)
        assert not await markdown.locator("a").evaluate_all(
            "links => links.some(link => /^(javascript|data):/i.test(link.getAttribute('href') ?? ''))"
        )
        assert await page.evaluate("window.__sessionMarkdownFixtureExecuted === undefined")
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_narration_is_visible(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "narration", viewport=viewport, capture_name=capture_name) as view:
        narration = view.page.locator('[data-fold-kind="narration"]')
        await expect(narration).to_be_visible()
        await _scroll_into_view(narration)
        await _capture(view)


async def test_history_opens_at_tail(history_view: VisualPage) -> None:
    page = history_view.page
    await page.wait_for_function(
        "() => {\n                const viewport = document.querySelector('[aria-label=\"Session transcript\"] .mantine-ScrollArea-viewport');\n                const latest = document.querySelector('[data-history-sequences~=\"17\"]');\n                if (!viewport || !latest || viewport.scrollHeight <= viewport.clientHeight) return false;\n                const box = viewport.getBoundingClientRect(), card = latest.getBoundingClientRect();\n                return Math.abs(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) <= 1\n                    && card.bottom > box.top && card.top < box.bottom;\n            }"
    )
    await _capture(history_view)


async def test_loading_older_history_preserves_anchor(history_view: VisualPage) -> None:
    page = history_view.page
    await page.wait_for_function(
        "() => {\n                const viewport = document.querySelector('[aria-label=\"Session transcript\"] .mantine-ScrollArea-viewport');\n                const latest = document.querySelector('[data-history-sequences~=\"17\"]');\n                if (!viewport || !latest || viewport.scrollHeight <= viewport.clientHeight) return false;\n                const box = viewport.getBoundingClientRect(), card = latest.getBoundingClientRect();\n                return Math.abs(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight) <= 1\n                    && card.bottom > box.top && card.top < box.bottom;\n            }"
    )
    thinking = page.locator('details[aria-label="Thinking"]')
    await expect(thinking).to_be_attached()
    await thinking.evaluate(
        "element => {\n                    const viewport = document.querySelector('[aria-label=\"Session transcript\"] .mantine-ScrollArea-viewport');\n                    viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 100;\n                    viewport.dispatchEvent(new Event('scroll'));\n                }"
    )
    await thinking.locator("summary").click()
    await wait_for_stable(page)
    original = await thinking.element_handle()
    top = await thinking.evaluate("element => element.getBoundingClientRect().top")
    # Loading older history must not scroll the reader to the off-screen load control.
    await page.get_by_role("button", name="Load older events").dispatch_event("click")
    await expect(page.locator('[data-history-sequences~="1"]').first).to_be_attached()
    await wait_for_stable(page)
    assert await thinking.evaluate("(element, original) => element === original", original)
    await expect(thinking).to_have_attribute("open", "")
    await expect(page.get_by_role("button", name="Load older events")).to_have_count(0)
    assert abs(await thinking.evaluate("element => element.getBoundingClientRect().top") - top) <= 1.5
    await _capture(history_view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_noisy_history(noisy_view: VisualPage) -> None:
    await _capture(noisy_view)


async def test_noisy_history_thinking(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    await page.locator('[data-tool-group-toggle][aria-expanded="false"]').first.click()
    thinking = page.locator('[data-fold-kind="thinking"]').first
    await thinking.locator("summary").click()
    await _scroll_into_view(thinking)
    await _capture(noisy_view)


async def test_noisy_raw_events(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    await page.get_by_role("button", name="Show raw event stream").click()
    rows = page.locator("[data-raw-event]")
    await expect(rows.first).to_be_attached()
    await expect(rows).to_have_count(await page.evaluate("window.__visualFixture__.noisyEventCount"))
    await page.locator(_TRANSCRIPT).evaluate("element => { element.scrollTop = 0; }")
    await _capture(noisy_view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_noisy_hook_filter(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    await page.get_by_role("button", name="Show raw event stream").click()
    rows = page.locator("[data-raw-event]")
    await expect(rows.first).to_be_attached()
    await page.get_by_label("Event kind").select_option("system · hook_response")
    await expect(rows.first).to_be_attached()
    await expect(rows.filter(has_not_text="hook_response")).to_have_count(0)
    await rows.first.locator("summary").click()
    await _scroll_into_view(rows.first)
    await expect(page.locator("[data-event-json]")).to_be_attached()
    await _capture(noisy_view)


async def test_sidebar_open(sidebar_view: VisualPage) -> None:
    page = sidebar_view.page
    await expect(page.locator('button[aria-controls="session-sidebar"]')).to_have_attribute("aria-expanded", "true")
    await _capture(sidebar_view)


async def test_sidebar_collapsed(sidebar_view: VisualPage) -> None:
    page = sidebar_view.page
    toggle = page.locator('button[aria-controls="session-sidebar"]')
    await toggle.click()
    await expect(toggle).to_have_attribute("aria-expanded", "false")
    await _capture(sidebar_view)


async def test_sidebar_resized(sidebar_view: VisualPage) -> None:
    page = sidebar_view.page
    separator = page.locator("[data-session-sidebar-resizer]")
    await separator.press("End")
    maximum = await separator.get_attribute("aria-valuemax")
    assert maximum is not None
    await expect(separator).to_have_attribute("aria-valuenow", maximum)
    # Capture the resized layout, not the keyboard focus outline on its drag handle.
    await separator.blur()
    await _capture(sidebar_view)


@pytest.mark.parametrize("viewport", [_PHONE], ids=["mobile"])
async def test_sidebar_mobile_drawer(sidebar_view: VisualPage) -> None:
    page = sidebar_view.page
    await page.locator('button[aria-controls="session-sidebar-mobile"]').press("Enter")
    await expect(page.locator(".mantine-Drawer-root #session-sidebar-mobile")).to_be_visible()
    await expect(page.locator(".mantine-Drawer-content")).to_have_css("opacity", "1")
    await page.wait_for_function(
        "() => document.querySelector('.mantine-Drawer-root').getAnimations({ subtree: true }).length === 0"
    )
    await expect(page.locator(".mantine-Drawer-close")).to_be_focused()
    await _capture(sidebar_view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_event_timeline_collapsed(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    strips = await _expect_compact_event_timeline(page)
    await _scroll_into_view(strips.first)
    await _capture(noisy_view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_event_timeline_expanded(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    strips = await _expect_compact_event_timeline(page)
    await strips.first.locator("[data-event-dot]").first.click()
    await strips.first.locator("[data-raw-event] summary").first.click()
    await expect(strips.first.locator("[data-event-json]")).to_be_attached()
    await _scroll_into_view(strips.first)
    await _capture(noisy_view)


async def test_activity_disclosures_collapsed(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    group = page.locator('[data-fold-kind="tool-group"]').first
    await expect(group).to_be_attached()
    assert await group.evaluate("element => element.getBoundingClientRect().height") <= 28
    await expect(group.locator('[data-fold-kind="tool-run"]')).to_have_count(0)
    await _scroll_into_view(group)
    await _capture(noisy_view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_activity_disclosures_expanded(noisy_view: VisualPage) -> None:
    page = noisy_view.page
    group = page.locator('[data-fold-kind="tool-group"]').first
    await expect(group).to_be_attached()
    assert await group.evaluate("element => element.getBoundingClientRect().height") <= 28
    await expect(group.locator('[data-fold-kind="tool-run"]')).to_have_count(0)
    await _click_right_edge(page, group.locator("[data-tool-group-toggle]"))
    await _click_right_edge(page, group.locator("[data-tool-run-toggle]").first)
    await expect(group.locator("[data-tool-file-preview]")).to_be_attached()
    await _scroll_into_view(group)
    await _capture(noisy_view)


@pytest.mark.parametrize(
    ("viewport", "color_scheme"),
    [(_DESKTOP, "light"), (_DESKTOP, "dark"), (_PHONE, "light")],
    ids=["desktop-light", "desktop-dark", "mobile-light"],
)
async def test_session_viewer(
    visual: VisualHarness, viewport: Viewport, capture_name: str, color_scheme: Literal["light", "dark"]
) -> None:
    async with _fixture(
        visual, "viewer", viewport=viewport, capture_name=capture_name, color_scheme=color_scheme
    ) as view:
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="tool-run"][data-tool-count="5"]')).to_be_attached()
        await expect(view.page.locator("[data-tool-run-toggle]").first).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_tool_result(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "tool-result", viewport=viewport, capture_name=capture_name) as view:
        await view.page.locator("[data-tool-run-toggle]").first.click()
        await _scroll_into_view(view.page.locator('[data-fold-kind="tool-run"]').first)
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(view.page.locator('[data-tool-name="Read"]')).to_be_attached()
        await expect(view.page.locator("[data-tool-output-image]")).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_read_file_result(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "file-result", viewport=viewport, capture_name=capture_name) as view:
        await view.page.locator("[data-tool-run-toggle]").first.click()
        await _scroll_into_view(view.page.locator('[data-fold-kind="tool-run"]').first)
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(view.page.locator('[data-tool-file-path="src/session-viewer.ts"]')).to_be_attached()
        await expect(view.page.locator("[data-tool-file-preview]")).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_subagent(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "subagent", viewport=viewport, capture_name=capture_name) as view:
        await view.page.locator("[data-tool-run-toggle]").first.click()
        await _scroll_into_view(view.page.locator('[data-fold-kind="tool-run"]').first)
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(view.page.locator('[data-subagent-activity][data-subagent-tool-count="2"]')).to_be_attached()
        await expect(view.page.locator('[data-subagent-latest-tool="Grep"]')).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_peer_hold(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "peer-hold", viewport=viewport, capture_name=capture_name) as view:
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="peer-message"][data-peer-from="plan-agent"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="peer-hold"][data-peer-state="held"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="peer-hold"][data-peer-state="dropped"]')).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_peer_message(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "peer-message", viewport=viewport, capture_name=capture_name) as view:
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(
            view.page.locator(
                '[data-fold-kind="peer-message"][data-peer-from="review-agent"][data-peer-handback="true"]'
            )
        ).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize("viewport", [_DESKTOP, _PHONE], ids=["desktop", "mobile"])
async def test_session_local_command_rows(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "local-commands", viewport=viewport, capture_name=capture_name) as view:
        await expect(view.page.locator('[aria-label="Session history"]')).to_be_attached()
        await expect(
            view.page.locator('[data-fold-kind="context"][data-context-model="claude-sonnet-4-5"]')
        ).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="stats"][data-stats-state="data"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="usage"]')).to_be_attached()
        await expect(view.page.locator('[data-fold-kind="status"]')).to_be_attached()
        await _capture(view)


async def test_session_sync_unpaired(visual: VisualHarness, viewport: Viewport, capture_name: str) -> None:
    async with _fixture(visual, "sync", viewport=viewport, capture_name=capture_name) as view:
        await expect(view.page.locator("#overview-heading")).to_be_attached()
        await expect(view.page.locator("#pairing-heading")).to_be_attached()
        await _capture(view)


@pytest.mark.parametrize(
    ("viewport", "color_scheme"), [(_DESKTOP, "dark"), (_PHONE, "light")], ids=["desktop-dark", "mobile-light"]
)
async def test_session_sync_paired(
    visual: VisualHarness, viewport: Viewport, capture_name: str, color_scheme: Literal["light", "dark"]
) -> None:
    async with _fixture(
        visual, "sync-paired", viewport=viewport, capture_name=capture_name, color_scheme=color_scheme
    ) as view:
        await expect(view.page.locator("#overview-heading")).to_be_attached()
        await expect(view.page.locator("#pairing-heading")).to_be_attached()
        await _capture(view)


if __name__ == "__main__":
    pytest_bazel.main()
