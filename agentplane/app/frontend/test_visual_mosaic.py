"""Visual acceptance for the experimental Mosaic shell."""

import pytest
import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.frontend.visual_app import AgentplaneFixture
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _mount(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.show_compact_pod_action()
    await app.mount_app("/mosaic")
    await expect(view.page.locator("[data-mosaic-pane]:visible").first).to_be_visible()
    await expect(view.page.get_by_role("heading", name="Mosaic")).to_be_visible()
    await expect(view.page.locator(".agentplane-sidebar")).to_have_count(0)
    await view.check(context="mosaic shell ready")


async def _add_pane(page: Page, label: str, kind: str) -> None:
    await page.get_by_role("button", name="Add pane").click()
    await page.get_by_role("textbox", name="Find a pane or thread").fill(label)
    await page.get_by_role("menuitem", name=label, exact=True).click()
    await expect(page.locator(f"[data-mosaic-pane-kind='{kind}']")).to_be_visible()


@pytest.mark.parametrize("viewport", [DESKTOP], ids=["desktop"])
async def test_desktop_shell_docks_the_existing_browsers(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await _mount(view, app)
    page = view.page
    await expect(page.locator("[data-mosaic-pane]")).to_have_count(3)
    await expect(page.locator("[data-mosaic-pane-kind='actions'] .agentplane-actions-sidebar-request")).to_have_count(3)
    await expect(
        page.locator("[data-mosaic-pane-kind='threads'] .agentplane-sidebar-group-label").first
    ).to_be_visible()
    await expect(page.locator("[data-mosaic-pane-kind='thread'] .agentplane-mosaic-thread-header")).to_be_visible()
    await view.capture(name="mosaic-desktop-integrated-shell", target=page.locator("#app"))

    actions = page.locator("[data-mosaic-pane-kind='actions']")
    await actions.get_by_role("button", name="Expand inspect running demo pods").click()
    await expect(actions.get_by_role("button", name="Approve inspect running demo pods")).to_be_visible()
    await expect(actions.get_by_role("button", name="Deny inspect running demo pods")).to_be_visible()
    await view.capture(name="mosaic-desktop-compact-quick-review", target=page.locator("#app"))

    await actions.get_by_role("button", name="View details for echo the test repository handle back").click()
    detail = page.locator("[data-mosaic-pane-kind='action']")
    await expect(detail).to_be_visible()
    await expect(page.locator("[data-mosaic-pane]")).to_have_count(4)
    thread_name = page.locator("[data-mosaic-pane-kind='thread']").get_by_role("textbox", name="Thread name")
    thread_name_box = await thread_name.bounding_box()
    assert thread_name_box is not None
    assert thread_name_box["width"] >= 100
    await page.mouse.move(0, 0)
    await view.capture(name="mosaic-desktop-action-detail", target=page.locator("#app"))
    await detail.get_by_role("button", name="Approve").click()
    await expect(detail).to_have_count(0)
    await expect(
        page.locator(".mosaic-window.agentplane-mosaic-active [data-mosaic-pane-kind='actions']")
    ).to_be_visible()
    await view.check(context="action decision returns to compact queue")

    await (
        page.locator("[data-mosaic-pane-kind='threads'] .agentplane-sidebar-row")
        .filter(has_text="Running thread")
        .click()
    )
    await expect(page.locator("[data-mosaic-pane-kind='thread']")).to_have_count(2)
    await view.capture(name="mosaic-desktop-two-open-threads", target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP], ids=["desktop"])
async def test_desktop_layout_resizes_and_persists(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await _mount(view, app)
    page = view.page
    splitter = page.locator(".agentplane-mosaic .mosaic-split").first
    await expect(splitter).to_be_visible()
    before = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    box = await splitter.bounding_box()
    assert box is not None
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2
    dx, dy = (60, 0) if box["height"] > box["width"] else (0, 60)
    await page.mouse.move(center_x, center_y)
    await page.mouse.down()
    await page.mouse.move(center_x + dx, center_y + dy, steps=8)
    await page.mouse.up()
    await page.wait_for_function(
        "before => localStorage.getItem('agentplane-mosaic-workspace-v1') !== before", arg=before
    )
    resized = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    assert resized is not None
    assert resized != before

    actions_title = page.locator(
        ".agentplane-mosaic .mosaic-window:has([data-mosaic-pane-kind='actions']) > .mosaic-window-toolbar .mosaic-window-title"
    )
    thread_body = page.locator(
        ".agentplane-mosaic .mosaic-window:has([data-mosaic-pane-kind='thread']) > .mosaic-window-body"
    )
    thread_box = await thread_body.bounding_box()
    assert thread_box is not None
    await actions_title.drag_to(
        thread_body, target_position={"x": thread_box["width"] - 12, "y": thread_box["height"] / 2}
    )
    await page.wait_for_function(
        "before => localStorage.getItem('agentplane-mosaic-workspace-v1') !== before", arg=resized
    )
    await view.capture(name="mosaic-desktop-docked-actions", target=page.locator("#app"))

    await _add_pane(page, "Action history", "history")
    saved = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    assert saved is not None
    await view.capture(name="mosaic-desktop-resized-with-history", target=page.locator("#app"))

    await page.evaluate("() => { window.location.hash = '/sandboxes'; }")
    await expect(page.locator(".agentplane-sidebar")).to_be_attached()
    await page.evaluate("() => { window.location.hash = '/mosaic'; }")
    await expect(page.locator("[data-mosaic-pane-kind='history']")).to_be_visible()
    restored = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    assert restored == saved
    await view.check(context="mosaic layout restored after route remount")
    await view.capture(name="mosaic-desktop-restored-layout", target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_shows_one_selected_pane(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await _mount(view, app)
    page = view.page
    await expect(page.locator("[data-mosaic-pane]:visible")).to_have_count(1)
    await expect(page.locator("[data-mosaic-pane-kind='thread']")).to_be_visible()
    await view.capture(name="mosaic-mobile-thread", target=page.locator("#app"))

    selector = page.get_by_role("combobox", name="Active pane")
    await selector.click()
    await page.get_by_role("option", name="Actions", exact=True).click()
    await expect(page.locator("[data-mosaic-pane-kind='actions']")).to_be_visible()
    await expect(page.locator("[data-mosaic-pane]:visible")).to_have_count(1)
    await view.capture(name="mosaic-mobile-compact-actions", target=page.locator("#app"))

    await (
        page.locator("[data-mosaic-pane-kind='actions']")
        .get_by_role("button", name="View details for echo the test repository handle back")
        .click()
    )
    await expect(page.locator("[data-mosaic-pane-kind='action']")).to_be_visible()
    await view.capture(name="mosaic-mobile-action-detail", target=page.locator("#app"))
    await page.get_by_role("button", name="Back to previous pane").click()
    await expect(page.locator("[data-mosaic-pane-kind='actions']")).to_be_visible()


if __name__ == "__main__":
    pytest_bazel.main()
