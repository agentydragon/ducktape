"""Agentplane navigation visual behavior tests."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.visual_app import IDLE_THREAD, RUNNING_THREAD, AgentplaneFixture
from agentplane.app.frontend.visual_assertions import _assert_phone_composer_layout
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, SMALL_MOBILE, Viewport
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.visual_fixtures")
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_workspace_docked_thread_panes(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.mount_app("/")
    await view.check(context="classic route ready")
    page = view.page
    if viewport == MOBILE:
        await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Workspace preview").click()
    await expect(page.get_by_role("heading", name="Workspace")).to_be_visible()
    await expect(page.locator(".agentplane-workspace-canvas")).to_be_visible()
    await expect(page.locator('.mosaic-window-title[title="Threads"]')).to_be_visible()

    await page.get_by_role("button", name="Add pane").click()
    thread_choice = page.locator(".agentplane-workspace-launcher").get_by_role("button", name=re.compile("Idle thread"))
    await thread_choice.click()
    await expect(page.locator('[aria-label="Thread history"]')).to_be_attached()

    if viewport == MOBILE:
        active_pane = page.get_by_label("Active pane", exact=True)
        await active_pane.select_option(label="Threads")
        await page.get_by_role("button", name="Close active pane").click()
    else:
        await page.get_by_role("button", name="Close Threads").click()

    await page.get_by_role("button", name="Add pane").click()
    running_choice = page.locator(".agentplane-workspace-launcher").get_by_role(
        "button", name=re.compile("Running thread")
    )
    await running_choice.click()
    await expect(page.locator('[aria-label="Thread history"]')).to_have_count(2)
    if viewport == MOBILE:
        await expect(active_pane).to_have_value(f"thread:{RUNNING_THREAD}")
        await expect(active_pane.locator("option")).to_have_text(["Idle thread", "Running thread"])
        await expect(page.locator(".agentplane-workspace-phone-switcher")).to_be_visible()
    else:
        await expect(page.locator('.mosaic-window-title[title="Idle thread"]')).to_be_visible()
        await expect(page.locator('.mosaic-window-title[title="Running thread"]')).to_be_visible()
    await view.capture(target=page.locator("#app"))
    await page.get_by_role("button", name="Add pane").click()
    await expect(page.locator(".agentplane-workspace-launcher")).to_be_visible()
    await expect(
        page.locator(".agentplane-workspace-launcher").get_by_role("textbox", name="Find a pane or thread")
    ).to_be_focused()
    launcher_capture = "pane-launcher-open-mobile" if viewport == MOBILE else "pane-launcher-open-desktop"
    await view.capture(launcher_capture, target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_current_sandbox_highlight(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    page = view.page
    if viewport == MOBILE:
        await page.get_by_role("button", name="Toggle navigation").click()
    current = page.locator('.agentplane-sidebar-group-link[aria-current="page"]')
    await expect(current).to_have_text("ready-sandbox")
    await expect(current.locator("xpath=..")).to_have_class(re.compile(r"\bcurrent\b"))
    await view.capture(target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_debug_tools_in_sidebar_footer(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    if viewport == MOBILE:
        await page.get_by_role("button", name="Toggle navigation").click()
    sidebar = page.get_by_role("navigation", name="Threads")
    debug = sidebar.locator(".agentplane-sidebar-footer").get_by_role("button", name="Debug tools")
    await expect(debug).to_be_visible()
    await debug.click()
    await expect(page.get_by_role("menuitem", name="Start recording")).to_be_visible()
    await view.capture(target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_navigation_drawer_threads_phone_drawer(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    await expect(page.locator("a.agentplane-sidebar-group-name").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_navigation_drawer_threads_failed_turn_phone_drawer(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.failed_turn(after_content=False)
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    await expect(page.locator('.agentplane-thread-status-indicator[data-status="turn_error"]').first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_navigation_drawer_threads_provisioning_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.add_provisioning_sandbox()
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    await expect(page.locator('a[href="#/sandboxes/test-provisioning"]').first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_navigation_drawer_threads_disconnected_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.disconnect_thread_stream()
    await app.age_outage(10000)
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
    await expect(page.locator('[data-connection="degraded"]').first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_drawer_covers_thread_controls(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_body=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
    await reasoning.locator(".agentplane-disclosure-summary").first.click()
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


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_drawer_covers_jump_to_latest(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.streaming_interleaved()
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
    # Probe where the actual high-z floating button was painted, not an arbitrary
    # uncovered patch of the drawer.
    assert await page.evaluate(
        "point => {\n            const top = document.elementFromPoint(point.x + point.width / 2, point.y + point.height / 2);\n            return top?.closest('.agentplane-sidebar-open') === document.querySelector('.agentplane-sidebar-open');\n        }",
        point,
    )
    await view.capture()


async def test_disconnected_threads_tooltip(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.disconnect_thread_stream()
    await app.age_outage(10000)
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    indicator = page.locator('[data-connection][aria-label*="reconnecting"]')
    await indicator.focus()
    await expect(page.get_by_text(re.compile("Threads: reconnecting since"))).to_be_visible()
    await view.capture()


async def test_paused_claude_in_sandbox_creation(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.pause_claude()
    await app.mount_app("/sandboxes?preset=public-coder")
    await view.page.wait_for_selector('[role="option"][data-combobox-disabled]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    harness = page.get_by_role("combobox", name="Harness")
    await expect(harness).to_have_value("Codex")
    await harness.click()
    await expect(page.locator('[role="option"][data-combobox-disabled]')).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


async def test_paused_claude_in_sandbox_details(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.pause_claude()
    await app.mount_app("/sandboxes/ready-sandbox")
    await view.page.wait_for_selector('[role="option"][data-combobox-disabled]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    harness = page.get_by_role("combobox", name="Harness")
    await expect(harness).to_have_value("Codex")
    await harness.click()
    await expect(page.locator('[role="option"][data-combobox-disabled]')).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [SMALL_MOBILE], ids=["small-mobile"])
async def test_phone_composer_controls_fit(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _assert_phone_composer_layout(page)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
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
