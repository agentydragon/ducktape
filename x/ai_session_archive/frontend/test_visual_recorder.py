"""On-device transcript recording across captured versions and sessions."""

import json
import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_transcript_recording(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "archive-recorder", viewport=Viewport(width=width, height=1000), color_scheme="light", capture_name=capture_name
    ) as view:
        page = view.page
        history = page.get_by_role("region", name="Conversation records", exact=True)
        await expect(page.get_by_role("heading", name="Compare two captured versions")).to_be_visible()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        assert (
            await page.evaluate(
                "performance.getEntriesByType('mark').filter(e => e.name.startsWith('ai-session-archive:history:')).length"
            )
            == 0
        )
        await page.get_by_role("button", name="Transcript recorder", exact=True).click()
        await expect(page.get_by_role("menuitem", name="Start recording", exact=True)).to_be_visible()
        await expect(page.get_by_role("menu")).to_have_css("opacity", "1")
        await view.capture(name=f"{capture_name}_idle_menu")
        await page.get_by_role("menuitem", name="Start recording", exact=True).click()
        await page.get_by_role("button", name="Transcript recorder (recording)", exact=True).click()
        await expect(page.get_by_role("menuitem", name="Mark a jump", exact=True)).to_be_visible()
        await expect(page.get_by_role("menu")).to_have_css("opacity", "1")
        await view.capture(name=f"{capture_name}_recording_menu")
        await page.get_by_role("menuitem", name="Mark a jump", exact=True).click()

        await page.get_by_role("button", name="Conversation options", exact=True).click()
        await page.locator('[role="menuitemradio"][data-version-id="codex-version-1"]').click()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(history).to_contain_text("This is the earlier captured version.")
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        await page.get_by_role("link", name=re.compile("Archive transcript UI work")).click()
        await expect(page.get_by_role("heading", name="Archive transcript UI work")).to_be_visible()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await history.locator("button[aria-expanded]").first.click()
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await page.get_by_role("button", name="Transcript recorder (recording)", exact=True).click()
        async with page.expect_download() as downloaded:
            await page.get_by_role("menuitem", name="Stop and download recording", exact=True).click()
        download = await downloaded.value
        assert download.suggested_filename.startswith("ai-session-archive-history-recording-")
        output = undeclared_outputs_dir() / f"{capture_name}.json"
        await download.save_as(output)
        recording = json.loads(output.read_text())
        assert recording["version"] == 4
        layouts = [entry["layout"] for entry in recording["layouts"]]
        scopes = {layout["scopeId"] for layout in layouts}
        assert "archive:codex-thread-1\x00codex-version-2" in scopes
        assert "archive:codex-thread-1\x00codex-version-1" in scopes
        assert "archive:codex-work-fixture\x00codex-work-version" in scopes
        assert len({layout["viewId"] for layout in layouts}) >= 3
        populated = [layout for layout in layouts if layout["mountedRows"]]
        assert populated
        assert all(layout["viewport"]["height"] > 0 for layout in populated)
        assert any(layout["plannedRows"] and layout["virtualRows"] for layout in populated)
        assert sum(mark["detail"]["kind"] == "marker" for mark in recording["marks"]) == 1
        assert all(mark["name"].startswith("ai-session-archive:history:") for mark in recording["marks"])
        assert any(mark["detail"]["kind"] == "click" for mark in recording["marks"])
        assert "I found two snapshots for this session." not in json.dumps(recording)
        assert "This is the earlier captured version." not in json.dumps(recording)
        await expect(page.get_by_role("button", name="Transcript recorder", exact=True)).to_be_visible()
        assert (
            await page.evaluate(
                "performance.getEntriesByType('mark').filter(e => e.name.startsWith('ai-session-archive:history:')).length"
            )
            == 0
        )
        await view.capture(name=f"{capture_name}_stopped")


if __name__ == "__main__":
    pytest_bazel.main()
