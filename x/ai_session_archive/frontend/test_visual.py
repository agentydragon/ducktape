"""Visual check for the local workstation session browser."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_session_list_and_captured_version(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "session-list-and-captured-version",
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
        await expect(progress).to_contain_text("Snapshots: 3 of 5 registered complete")
        await expect(progress).to_contain_text("Codex metadata: 3 pending · 0 failed · 1 partial")
        await expect(progress).to_contain_text("Codex metadata failed")
        await expect(progress).to_contain_text("File entries: 42 processed · 9 pending · 2 failed")
        await expect(progress).to_contain_text("File entries processed: 42 of 53 inventoried")
        await expect(progress).to_contain_text("Snapshot counts include registered snapshots")
        rugged_progress = page.get_by_role("progressbar", name="Rugged file-entry progress")
        await expect(rugged_progress).to_have_attribute("aria-valuenow", "42")
        await expect(rugged_progress).to_have_attribute("aria-valuemax", "53")
        await expect(rugged_progress).to_have_attribute(
            "aria-valuetext", "42 processed, 9 pending, 2 failed of 53 inventoried file entries"
        )
        await expect(page.get_by_text("ThinkPad", exact=True).first).to_be_visible()
        await expect(page.get_by_role("heading", name="Compare two captured versions")).to_be_visible()
        await expect(page.locator('[aria-label="Session transcript"]')).to_contain_text(
            "I found two snapshots for this session."
        )
        await expect(
            page.locator('[aria-label="Session transcript"] select[aria-label="Captured version"]')
        ).to_contain_text("Latest captured")
        await view.check(context="machine list and captured version are visible")
        await view.capture(target=page.locator("#root"))


async def test_import_progress_on_mobile(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "import-progress-mobile",
        viewport=Viewport(width=390, height=844),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        progress = page.get_by_role("region", name="Import progress by machine")
        thinkpad_progress = page.get_by_role("progressbar", name="ThinkPad file-entry progress")
        sidebar = page.get_by_role("complementary", name="Session list")
        session_list_refresh = page.get_by_role("button", name="Refresh session list")
        progress_explanation = page.locator(".session-import-progress-explanation-mobile")
        await expect(progress).to_contain_text("File entries: 27 processed · 4 pending · 0 failed")
        await expect(thinkpad_progress).to_have_attribute("aria-valuemax", "31")
        await expect(session_list_refresh).to_be_visible()
        await expect(progress_explanation.locator("summary")).to_contain_text(
            "Denominator: inventoried entries so far; can grow"
        )

        progress_bounds = await thinkpad_progress.bounding_box()
        sidebar_bounds = await sidebar.bounding_box()
        refresh_bounds = await session_list_refresh.bounding_box()
        assert progress_bounds is not None
        assert progress_bounds["x"] >= 0
        assert progress_bounds["x"] + progress_bounds["width"] <= 390
        assert progress_bounds["y"] >= 0
        assert progress_bounds["y"] + progress_bounds["height"] <= 844
        assert sidebar_bounds is not None
        assert refresh_bounds is not None
        assert refresh_bounds["y"] + refresh_bounds["height"] <= sidebar_bounds["y"] + sidebar_bounds["height"]

        await view.check(context="the first machine progress bar and session-list controls fit the mobile viewport")
        await view.capture(target=page.locator("#root"))


async def test_inferred_subagent_parent_link(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "inferred-subagent-parent-link",
        viewport=Viewport(width=1440, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await page.get_by_role("link", name="Inspect a local transcript").click()
        related = page.locator('[aria-label="Related sessions"]')
        await expect(related).to_contain_text("Parent inferred from Claude subagent path")
        await expect(related).to_contain_text("Inferred Claude parent · session 12ab")
        await expect(
            page.locator('[aria-label="Session transcript"] select[aria-label="Captured version"]')
        ).to_be_visible()
        await view.check(context="child transcript and inferred parent link are visible")
        await view.capture(target=page.locator("#root"))


if __name__ == "__main__":
    pytest_bazel.main()
