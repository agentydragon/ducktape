"""Sidebar metadata follows pane width; conversation metadata shares the cwd row."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_compact_session_metadata(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "compact-session-metadata",
        viewport=Viewport(width=width, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Compare two captured versions")).to_be_visible()
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        row = page.get_by_role("link", name=re.compile("Compare two captured versions"))
        await expect(row).to_be_visible()
        await view.capture(name=f"{capture_name}_sidebar")
        if width > 600:
            resize = page.get_by_role("separator", name="Resize session list", exact=True)
            await resize.focus()
            await resize.press("End")
            harness = await row.get_by_text("Codex CLI", exact=True).bounding_box()
            date = await row.locator(".session-row-time").bounding_box()
            assert harness is not None
            assert date is not None
            assert abs(harness["y"] - date["y"]) < 1
            assert harness["x"] + harness["width"] <= date["x"]
            await view.capture(name=f"{capture_name}_wide_sidebar")
            await resize.press("Home")
            harness = await row.get_by_text("Codex CLI", exact=True).bounding_box()
            title = await row.get_by_text("Compare two captured versions", exact=True).bounding_box()
            assert harness is not None
            assert title is not None
            assert harness["y"] >= title["y"] + title["height"]
            assert await row.evaluate("element => element.scrollWidth <= element.clientWidth")
            await view.capture(name=f"{capture_name}_narrow_sidebar")
            await resize.press("End")
        else:
            await row.click()

        transcript = page.get_by_role("region", name="Session transcript", exact=True)
        cwd = await transcript.get_by_text("/home/example/project", exact=True).bounding_box()
        dates = await transcript.get_by_text(re.compile(r"^Started .*Last active")).bounding_box()
        assert cwd is not None
        assert dates is not None
        if width > 600:
            assert abs(cwd["y"] - dates["y"]) < 2
            assert cwd["x"] + cwd["width"] <= dates["x"]
        await view.capture(name=f"{capture_name}_conversation_metadata")


if __name__ == "__main__":
    pytest_bazel.main()
