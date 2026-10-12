"""Agentplane navigation visual behavior tests."""

import re

import pytest
import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.frontend.testing.visual_assertions import (
    _assert_phone_composer_layout,
    _open_phone_navigation,
    _open_standalone_reasoning,
)
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE, MOBILE_ONLY
from agentplane.app.frontend.visual_app import IDLE_THREAD, RUNNING_THREAD, AgentplaneFixture, Flag, Scene
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import SMALL_MOBILE
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_archived_thread_toggle(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.locator(".agentplane-sidebar-archived-toggle label").click()
    await expect(page.get_by_role("switch", name="Show archived threads")).to_be_checked()
    await expect(page.locator('.agentplane-thread-status-indicator[data-status="archived"]')).to_be_visible()
    await page.mouse.move(0, 0)
    await expect(page.get_by_role("tooltip")).to_have_count(0)
    await view.capture(target=view.page.locator("#app"))


async def expect_notice_badges(page: Page, *, stale: bool) -> None:
    """The fixture's two unprocessed-count badges and one failed-delivery badge, all painted, and
    faded exactly when the notice stream is down."""
    badges = page.locator(".agentplane-sidebar-row-notices")
    for state, count in [("pending", 2), ("error", 1)]:
        await expect(badges.and_(page.locator(f'[data-state="{state}"]'))).to_have_count(count)
    for badge in await badges.all():
        await expect(badge).to_be_visible()
        await expect(badge).to_have_css("opacity", "0.5" if stale else "1")


async def test_thread_notice_badges(view: VisualPage, app: AgentplaneFixture) -> None:
    """Unprocessed-entry counts and a failed delivery on Thread rows; an empty inbox shows nothing."""
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await expect_notice_badges(page, stale=False)
    await view.capture(target=page.get_by_role("navigation", name="Threads"))


@DESKTOP_AND_MOBILE
async def test_current_sandbox_highlight(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    current = page.locator('.agentplane-sidebar-group-link[aria-current="page"]')
    await expect(current).to_have_text("ready-sandbox")
    await expect(current.locator("xpath=..")).to_have_class(re.compile(r"\bcurrent\b"))
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_debug_tools_in_sidebar_footer(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    sidebar = page.get_by_role("navigation", name="Threads")
    debug = sidebar.locator(".agentplane-sidebar-footer").get_by_role("button", name="Debug tools")
    await expect(debug).to_be_visible()
    await debug.click()
    await expect(page.get_by_role("menuitem", name="Start recording")).to_be_visible()
    await view.capture(target=page.locator("#app"))


async def open_phone_drawer(view: VisualPage, app: AgentplaneFixture) -> Page:
    """Mount the thread list and open its navigation drawer."""
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    return page


@MOBILE_ONLY
async def test_mobile_navigation_drawer_threads_phone_drawer(view: VisualPage, app: AgentplaneFixture) -> None:
    page = await open_phone_drawer(view, app)
    await expect(page.locator("a.agentplane-sidebar-group-name").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_mobile_navigation_drawer_threads_failed_turn_phone_drawer(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.failed_turn(after_content=False)
    page = await open_phone_drawer(view, app)
    await expect(page.locator('.agentplane-thread-status-indicator[data-status="turn_error"]').first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_mobile_navigation_drawer_threads_provisioning_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ADD_PROVISIONING_SANDBOX)
    page = await open_phone_drawer(view, app)
    await expect(page.locator('a[href="#/sandboxes/test-provisioning"]').first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_mobile_navigation_drawer_threads_disconnected_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.THREAD_STREAM_DROPPED)
    await app.age_outage(10000)
    page = await open_phone_drawer(view, app)
    await expect(page.locator('[data-connection="degraded"]').first).to_be_visible()
    await expect_notice_badges(page, stale=True)
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_mobile_drawer_covers_thread_controls(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_body=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_standalone_reasoning(page)
    history = page.locator("[aria-label='Thread history']")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.evaluate("element => { element.scrollTop = element.scrollHeight / 2; }")
    await wait_for_stable(page)
    await page.get_by_role("button", name="Toggle navigation").click()
    drawer = page.locator(".agentplane-sidebar-open")
    await expect(drawer).to_be_visible()
    # A sticky disclosure heading (z=100) or Jump to latest (z=101) in the main
    # column must never punch through the drawer's lower numeric z-index.
    assert await page.evaluate(
        "() => {\n            const drawer = document.querySelector('.agentplane-sidebar-open');\n            const main = document.querySelector('.agentplane-shell-main');\n            if (!drawer || !main) return false;\n            return getComputedStyle(main).zIndex === '0' &&\n                getComputedStyle(drawer).zIndex === '30' &&\n                document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.closest('.agentplane-sidebar') === drawer;\n        }"
    )
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_mobile_drawer_covers_jump_to_latest(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.STREAMING_INTERLEAVED)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    history = page.locator("[aria-label='Thread history']")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    # The fixture's folded run is short; open it to create real scroll distance
    # before the reader leaves the bottom of this still-running thread.
    await history.locator(".agentplane-disclosure-summary").filter(has_text="32 tool calls").first.click()
    await history.hover()
    await page.mouse.wheel(0, -2500)
    jump = page.get_by_role("button", name="Jump to latest")
    await expect(jump).to_be_visible()
    point = await jump.bounding_box()
    assert point is not None
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    await expect_notice_badges(page, stale=False)
    # Probe where the actual high-z floating button was painted, not an arbitrary
    # uncovered patch of the drawer.
    assert await page.evaluate(
        "point => {\n            const top = document.elementFromPoint(point.x + point.width / 2, point.y + point.height / 2);\n            return top?.closest('.agentplane-sidebar-open') === document.querySelector('.agentplane-sidebar-open');\n        }",
        point,
    )
    await view.capture()


async def test_disconnected_threads_tooltip(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.THREAD_STREAM_DROPPED)
    await app.age_outage(10000)
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    indicator = page.locator('[data-connection][aria-label*="reconnecting"]')
    await indicator.focus()
    await expect(page.get_by_text(re.compile("Threads: reconnecting since"))).to_be_visible()
    await view.capture()


async def capture_paused_claude_harness_picker(view: VisualPage, app: AgentplaneFixture, route: str) -> None:
    """With Claude paused, the harness picker defaults to Codex and offers Claude disabled."""
    await app.set_flag(Flag.CLAUDE_PAUSED)
    await app.mount_app(route)
    await view.page.wait_for_selector('[role="option"][data-combobox-disabled]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    harness = page.get_by_role("combobox", name="Harness")
    await expect(harness).to_have_value("Codex")
    await harness.click()
    await expect(page.locator('[role="option"][data-combobox-disabled]')).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


async def test_paused_claude_in_sandbox_creation(view: VisualPage, app: AgentplaneFixture) -> None:
    await capture_paused_claude_harness_picker(view, app, "/sandboxes?preset=public-coder")


async def test_paused_claude_in_sandbox_details(view: VisualPage, app: AgentplaneFixture) -> None:
    await capture_paused_claude_harness_picker(view, app, "/sandboxes/ready-sandbox")


@pytest.mark.parametrize("viewport", [SMALL_MOBILE], ids=["small-mobile"])
async def test_phone_composer_controls_fit(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _assert_phone_composer_layout(page)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_composer_more_menu(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="More", exact=True).click()
    await expect(page.get_by_role("menuitem", name="Debug history")).to_be_visible()
    await expect(page.get_by_role("menu")).to_be_visible()
    await expect(page.locator('[data-thread-anchor="34"]')).to_be_attached()
    await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
