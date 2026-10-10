"""Visual acceptance for the experimental docked-pane route."""

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


async def _mount_mosaic(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_app("/mosaic")
    await expect(view.page.locator("[data-mosaic-pane-kind='action'] button[aria-label^='Approve']")).to_be_attached()
    await expect(view.page.locator("[data-mosaic-pane]")).to_have_count(3)
    await expect(view.page.get_by_role("button", name="Mosaic preview")).to_be_visible()
    await view.check(context="mosaic fixture ready")


async def _add_pane(page: Page, label: str, kind: str) -> None:
    await page.get_by_role("button", name="Add pane").click()
    await page.get_by_role("textbox", name="Find a pane or thread").fill(label)
    await page.get_by_role("menuitem", name=label, exact=True).click()
    await expect(page.locator(f"[data-mosaic-pane-kind='{kind}']")).to_be_attached()


@pytest.mark.parametrize("viewport", [DESKTOP], ids=["desktop"])
async def test_desktop_shows_two_threads_and_an_action(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await _mount_mosaic(view, app)
    panes = view.page.locator("[data-mosaic-pane]")
    for index in range(await panes.count()):
        await expect(panes.nth(index)).to_be_visible()
    await expect(view.page.locator(".agentplane-topbar-actions [aria-label='More']")).to_have_count(0)
    await view.capture(name="mosaic-desktop-two-threads-and-action", target=view.page.locator("#app"))

    page = view.page
    source_grip = page.locator(".agentplane-mosaic-pane-grip").nth(2)
    target_pane = page.locator("[data-mosaic-pane-kind='thread']").first
    source_box = await source_grip.bounding_box()
    target_box = await target_pane.bounding_box()
    assert source_box is not None
    assert target_box is not None
    await page.mouse.move(source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
    await page.mouse.down()
    await page.mouse.move(target_box["x"] + target_box["width"] / 2, target_box["y"] + 2, steps=8)
    await page.mouse.up()

    action_pane = page.locator("[data-mosaic-pane-kind='action']")
    await expect(action_pane).to_be_visible()
    action_box = await action_pane.bounding_box()
    target_box = await target_pane.bounding_box()
    assert action_box is not None
    assert target_box is not None
    assert action_box["x"] == pytest.approx(target_box["x"], abs=2)
    assert action_box["y"] < target_box["y"]

    horizontal_splitter = page.locator("[data-mosaic-resize='horizontal']").first
    initial_ratio = int(await horizontal_splitter.get_attribute("aria-valuenow") or "0")
    splitter_box = await horizontal_splitter.bounding_box()
    assert splitter_box is not None
    await page.mouse.move(splitter_box["x"] + splitter_box["width"] / 2, splitter_box["y"] + 5)
    await page.mouse.down()
    await page.mouse.move(splitter_box["x"] + splitter_box["width"] / 2 + 48, splitter_box["y"] + 5, steps=5)
    await page.mouse.up()
    await expect(horizontal_splitter).not_to_have_attribute("aria-valuenow", str(initial_ratio))
    await horizontal_splitter.focus()
    current_ratio = int(await horizontal_splitter.get_attribute("aria-valuenow") or "0")
    await page.keyboard.press("ArrowRight")
    await expect(horizontal_splitter).to_have_attribute("aria-valuenow", str(min(85, current_ratio + 5)))

    saved_layout = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    assert saved_layout is not None
    # The visual harness clears localStorage at page startup. Navigate away and back to remount
    # MosaicView without that test-harness reset, then confirm its browser-local saved tree returns.
    await page.evaluate("() => { window.location.hash = '/sandboxes'; }")
    await expect(page.locator(".agentplane-mosaic")).to_have_count(0)
    await page.evaluate("() => { window.location.hash = '/mosaic'; }")
    await expect(page.locator("[data-mosaic-pane]")).to_have_count(3)
    await expect(page.locator("[data-mosaic-pane-kind='action']")).to_be_visible()
    restored_layout = await page.evaluate("() => localStorage.getItem('agentplane-mosaic-workspace-v1')")
    assert restored_layout == saved_layout
    await view.check(context="mosaic dock and resize persisted after route remount")
    await view.capture(name="mosaic-desktop-docked-resized-persisted", target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP], ids=["desktop"])
async def test_desktop_browses_threads_actions_and_history(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await _mount_mosaic(view, app)
    page = view.page
    panes = page.locator("[data-mosaic-pane]")
    while await panes.count() > 0:
        await panes.first.locator("button[aria-label^='Close']").click()
    await expect(panes).to_have_count(0)

    await _add_pane(page, "Threads", "threads")
    await _add_pane(page, "Actions", "actions")
    await _add_pane(page, "Action history", "history")
    await expect(page.locator("[data-mosaic-pane-kind='threads'] [aria-label='Filter threads']")).to_be_visible()
    await expect(page.locator("[data-mosaic-pane-kind='actions']")).to_contain_text("Pending (2)")
    await expect(
        page.locator("[data-mosaic-pane-kind='history'] [aria-label^='Open details for']").first
    ).to_be_visible()
    await view.check(context="thread browser, pending actions and action history panes ready")
    await view.capture(name="mosaic-desktop-thread-browser-actions-history", target=page.locator("#app"))

    actions_pane = page.locator("[data-mosaic-pane-kind='actions']")
    await actions_pane.get_by_role("button", name="Open details for echo the test repository handle back").click()
    detail_pane = page.locator("[data-mosaic-pane-kind='action']")
    await expect(detail_pane).to_be_attached()
    await detail_pane.get_by_role("button", name="Approve").click()
    await expect(detail_pane).to_have_count(0)
    await expect(page.get_by_role("combobox", name="Active pane")).to_have_value("actions")
    await view.check(context="resolved action returned to its originating pane")
    await view.capture(name="mosaic-desktop-action-decision-returns-to-actions", target=page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_switches_one_active_pane(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await _mount_mosaic(view, app)
    page = view.page
    await expect(page.locator(".agentplane-mosaic-pane-grip").first).to_be_hidden()
    await expect(page.locator(".agentplane-mosaic-resize-handle").first).to_be_hidden()
    active_selector = page.get_by_role("combobox", name="Active pane")
    await active_selector.click()
    await page.get_by_role("option", name="echo the test repository handle back").click()
    action_pane = page.locator("[data-mosaic-pane-kind='action'].is-active")
    await expect(action_pane).to_be_visible()
    await expect(page.locator("[data-mosaic-pane]:visible")).to_have_count(1)
    await view.capture(name="mosaic-mobile-action-details", target=page.locator("#app"))

    await active_selector.click()
    await page.get_by_role("option").first.click()
    await expect(page.locator("[data-mosaic-pane-kind='thread'].is-active")).to_be_visible()
    await expect(page.locator("[data-mosaic-pane]:visible")).to_have_count(1)
    await expect(page.locator(".agentplane-topbar-title").get_by_role("heading", name="Mosaic")).to_be_visible()
    await view.capture(name="mosaic-mobile-thread", target=page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
