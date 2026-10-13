"""Dockable workspace visual behavior tests."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@DESKTOP_AND_MOBILE
async def test_dock_opens_thread_beside_browser(view: VisualPage, app: AgentplaneFixture) -> None:
    """Desktop docks the Thread browser, the Actions queue and an opened Thread side by side; a phone
    shows the opened Thread alone, the other panes one pick away."""
    await app.mount_app("/dock")
    await view.check(context="fixture ready")
    page = view.page
    await page.locator(".agentplane-thread-list-row", has_text="Idle thread").click()
    await expect(page.get_by_role("textbox", name="Thread name")).to_have_value("Idle thread")
    await view.capture(target=page.locator("#app"))


async def test_dock_restores_saved_workspace(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/dock")
    await view.check(context="fixture ready")
    page = view.page
    await page.locator(".agentplane-thread-list-row", has_text="Idle thread").click()
    await expect(page.locator(".dv-tab", has_text="Idle thread")).to_be_visible()
    await page.locator(".agentplane-thread-list-row", has_text="Running thread").click()
    await expect(page.locator(".dv-tab", has_text="Running thread")).to_be_visible()
    await app.mount_app("/dock")
    await view.check(context="remounted")
    await expect(page.locator(".dv-tab", has_text="Idle thread")).to_be_visible()
    await expect(page.locator(".dv-tab", has_text="Running thread")).to_be_visible()
    await view.capture(target=page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
