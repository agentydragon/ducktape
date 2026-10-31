"""Agentplane navigation visual behavior tests."""

import json
import re

import pytest
import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.frontend.testing.app import IDLE_THREAD, RUNNING_THREAD, AgentplaneFixture, Flag, Scene
from agentplane.app.frontend.testing.visual_assertions import (
    _assert_phone_composer_layout,
    _open_phone_navigation,
    _open_standalone_reasoning,
)
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE, MOBILE_ONLY
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import SMALL_MOBILE, Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_archived_thread_toggle(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    sidebar = page.get_by_role("navigation")
    await sidebar.locator(".agentplane-thread-list-archived-toggle label").click()
    await expect(sidebar.get_by_role("switch", name="Show archived threads")).to_be_checked()
    await expect(sidebar.locator('.agentplane-thread-status-indicator[data-status="archived"]')).to_be_visible()
    await page.mouse.move(0, 0)
    await expect(page.get_by_role("tooltip")).to_have_count(0)
    await view.capture(target=view.page.locator("#app"))


async def expect_notice_badges(page: Page, *, stale: bool) -> None:
    """The fixture's two unprocessed-count badges and one failed-delivery badge in the sidebar, all
    painted, and faded exactly when the notice stream is down."""
    badges = page.get_by_role("navigation").locator(".agentplane-thread-list-row-notices")
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
    await view.capture(target=page.get_by_role("navigation"))


@DESKTOP_AND_MOBILE
async def test_current_sandbox_highlight(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    current = page.locator('.agentplane-thread-list-group-link[aria-current="page"]')
    await expect(current.locator(".agentplane-thread-list-group-name")).to_have_text("ready-sandbox")
    await expect(current).to_have_class(re.compile(r"\bcurrent\b"))
    await view.capture(target=page.locator("#app"))


async def test_vm_console_to_threads_tab(view: VisualPage, app: AgentplaneFixture) -> None:
    """Leaving the live VM console for Threads unmounts its stream and keeps the page mounted."""
    await app.as_vm_sandbox("ready-sandbox")
    await app.mount_app("/sandboxes/ready-sandbox?tab=console")
    page = view.page
    await expect(page.get_by_role("tab", name="Console")).to_have_attribute("aria-selected", "true")
    await expect(page.get_by_label("Console output")).to_be_visible()
    await expect(page.get_by_label("Console output")).to_contain_text("guest console output")
    await view.check(context="console attached before changing tabs")
    await view.capture(name="vm-console-before-threads", target=page.locator("#app"))

    await page.get_by_role("tab", name="Threads").click()
    await expect(page.get_by_role("tab", name="Threads")).to_have_attribute("aria-selected", "true")
    await expect(page.get_by_role("button", name="New thread", exact=True)).to_be_visible()
    await view.check(context="after changing from VM console to Threads")
    await view.capture(name="vm-console-to-threads", target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_pod_sandbox_hides_vm_console(view: VisualPage, app: AgentplaneFixture) -> None:
    """A Pod Sandbox has no Console tab, including when a link requests the VM-only tab."""
    await app.mount_app("/sandboxes/ready-sandbox?tab=console")
    page = view.page
    await expect(page.get_by_label("Sandbox status: Ready")).to_be_visible()
    await expect(page.get_by_role("tab", name="Threads")).to_have_attribute("aria-selected", "true")
    await expect(page.get_by_role("tab", name="Console")).to_have_count(0)
    await expect(page.get_by_label("Console output")).to_have_count(0)
    await view.check(context="Pod Sandbox opened without the VM console")
    await view.capture(target=page.locator("#app"))


async def test_queued_session_open_follows_live_projection_and_survives_return(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    """A queued Open follows its History projection; the Sandbox table lists it on return."""
    await app.queue_open_response()
    await app.mount_app("/sandboxes/ready-sandbox?tab=threads")
    page = view.page
    await page.get_by_role("button", name="New thread", exact=True).click()
    await page.get_by_role("button", name="Create thread").click()
    pending = page.get_by_role("status").filter(has_text="Open attempt")
    await expect(pending).to_contain_text("Queued in Dispatcher")
    match = re.search(r"Open attempt ([0-9a-f-]{36})", await pending.inner_text())
    assert match is not None
    session_id = match.group(1)

    await app.project_queued_session()
    await expect(page).to_have_url(re.compile(re.escape(session_id)))
    sidebar_thread = page.locator(".agentplane-thread-list-row-name").filter(has_text="Queued opening thread")
    await expect(sidebar_thread).to_be_visible()

    await page.locator("a.agentplane-thread-list-group-link").filter(has_text="ready-sandbox").click()
    await page.get_by_role("tab", name="Threads").click()
    row = page.get_by_role("row", name=re.compile("Queued opening thread"))
    await expect(row).to_be_visible()
    await expect(
        page.locator(".agentplane-thread-list-row-name").filter(has_text="Queued opening thread")
    ).to_be_visible()
    await view.check(context="queued Thread projected and listed after returning to its Sandbox")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_thread_view_recorder_in_sidebar_footer(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    sidebar = page.get_by_role("navigation")
    debug = sidebar.locator(".agentplane-sidebar-footer").get_by_role("button", name="Thread view recorder")
    await expect(debug).to_be_visible()
    await debug.click()
    await expect(page.get_by_role("menuitem", name="Start recording")).to_be_visible()
    await view.capture(target=page.locator("#app"))


async def test_thread_view_recording_contains_thread_layouts(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_body=True)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    history = page.get_by_role("region", name="Thread history", exact=True)
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await page.get_by_role("button", name="Thread view recorder", exact=True).click()
    await page.get_by_role("menuitem", name="Start recording", exact=True).click()
    await page.locator(".agentplane-thread-list-row-name", has_text="Running thread").click()
    await expect(page.get_by_role("textbox", name="Thread name", exact=True)).to_have_value("Running thread")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    # Make a real scroll correction necessary; compact histories need no scroll write.
    await _open_standalone_reasoning(page)
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.hover()
    await page.mouse.wheel(0, -300)
    await page.get_by_role("button", name="Jump to latest", exact=True).click()
    await page.get_by_role("button", name="Thread view recorder (recording)", exact=True).click()
    async with page.expect_download() as downloaded:
        await page.get_by_role("menuitem", name="Stop and download recording", exact=True).click()
    download = await downloaded.value
    assert download.suggested_filename.startswith("agentplane-thread-view-recording-")
    path = await download.path()
    assert path is not None
    recording = json.loads(path.read_text())
    assert recording["version"] == 3
    layouts = recording["layouts"]
    assert {entry["layout"]["threadId"] for entry in layouts} == {IDLE_THREAD, RUNNING_THREAD}
    assert len({entry["layout"]["viewId"] for entry in layouts}) == 2
    assert any(entry["phase"] == "frame" for entry in layouts)
    assert any(entry["phase"] == "virtual-change" for entry in layouts)
    assert any(entry["phase"] == "scroll-after" and entry["reason"] == "follow-committed-layout" for entry in layouts)
    populated = [entry["layout"] for entry in layouts if entry["layout"]["mountedRows"]]
    assert populated
    assert all(layout["viewport"]["height"] > 0 for layout in populated)
    assert any(layout["virtualRows"] and layout["plannedRows"] for layout in populated)
    assert "List the repository files." not in json.dumps(recording)
    await expect(page.get_by_role("button", name="Thread view recorder", exact=True)).to_be_visible()
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_system_status_page(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    """The System page shows each component and its reason in desktop and phone layouts."""
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    await page.get_by_role("button", name="System status").click()
    rows = page.locator(".agentplane-system-status-table tbody tr")
    healthy = rows.filter(has_text=re.compile("Healthy component"))
    down = rows.filter(has_text=re.compile("Down component"))
    await expect(healthy.get_by_text("ok", exact=True)).to_be_visible()
    await expect(down.get_by_text("down", exact=True)).to_be_visible()
    await expect(down).to_contain_text("UNAVAILABLE: connection refused")
    if viewport.width <= 560:
        await expect(down).to_have_css("display", "grid")
        table = page.locator(".agentplane-system-status-table")
        assert await table.evaluate("element => element.scrollWidth <= element.clientWidth")
    await wait_for_stable(page)
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
    drawer = page.locator(".agentplane-sidebar-open")
    group_names = drawer.locator("a.agentplane-thread-list-group-link .agentplane-thread-list-group-name")
    await expect(group_names.first).to_be_visible()
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
    await expect(page.locator('.agentplane-sidebar-open a[href="#/sandboxes/test-provisioning"]')).to_be_visible()
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
    await page.get_by_role("button", name="New thread", exact=True).click()
    harness = page.get_by_role("combobox", name="Harness")
    await expect(harness).to_have_value("Codex")
    await harness.click()
    await expect(page.locator('[role="option"][data-combobox-disabled]')).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


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


@DESKTOP_AND_MOBILE
async def test_long_thread_name_sandbox_label(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport, capture_name: str
) -> None:
    """A phone's topbar gives a long thread name the row and drops the Sandbox label, which the More
    menu still lists; a desktop topbar has room for both."""
    await app.name_thread(IDLE_THREAD, "Idle thread whose long name leaves its Sandbox no room in a phone topbar")
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    label = page.locator(".agentplane-thread-heading-sandbox")
    if viewport.width <= 560:
        await expect(label).not_to_be_in_viewport()
    else:
        await expect(label).to_be_in_viewport()
    await view.capture(f"{capture_name}-topbar", target=page.locator(".agentplane-topbar"))
    await page.get_by_role("button", name="More", exact=True).click()
    await expect(page.get_by_role("menu")).to_contain_text("Sandbox: ready-sandbox")
    await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
