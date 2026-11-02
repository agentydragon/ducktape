"""Render fixture states with explicit Python readiness and capture."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.frontend_visual.capture import VisualHarness
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("scene", ["main_page", "streak_rest", "active_bonus_countdown", "active_bonus_unlocked"])
async def test_main_page(visual: VisualHarness, scene: str, capture_name: str) -> None:
    async with visual.open(scene, viewport=Viewport(height=1400, width=1200), capture_name=capture_name) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.page.wait_for_selector('[data-testid="sync-banner-offline"]', state="attached")
        await view.capture(scene, target=view.page.locator("#app"))


async def test_changelog(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open("changelog", viewport=Viewport(height=900, width=1200), capture_name=capture_name) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.capture("changelog", target=view.page.locator("#app"))


async def test_session_award(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open("session_award", viewport=Viewport(), capture_name=capture_name) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.capture("session_award", target=view.page.locator("#shot"))


if __name__ == "__main__":
    pytest_bazel.main()
