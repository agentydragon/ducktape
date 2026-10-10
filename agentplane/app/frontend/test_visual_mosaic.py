"""Visual acceptance for the experimental docked-pane route."""

import pytest
import pytest_bazel
from playwright.async_api import expect

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
    await view.check(context="mosaic fixture ready")


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


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_mobile_switches_one_active_pane(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await _mount_mosaic(view, app)
    page = view.page
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
    await view.capture(name="mosaic-mobile-thread", target=page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
