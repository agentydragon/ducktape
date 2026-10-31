"""Compact Claude tool steps, including records that also carry visible prose."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("width", [1440, 390], ids=["desktop", "mobile"])
async def test_claude_paired_steps(visual: VisualHarness, capture_name: str, width: int) -> None:
    async with visual.open(
        "claude-paired-steps",
        viewport=Viewport(width=width, height=1000),
        color_scheme="light",
        capture_name=capture_name,
    ) as view:
        page = view.page
        await expect(page.get_by_role("heading", name="Local agent sessions")).to_be_visible()
        if width < 600:
            await page.get_by_role("button", name="Session list", exact=True).click()
        await page.get_by_role("link", name=re.compile("Claude paired tools and mixed messages")).click()
        history = page.get_by_role("region", name="Conversation records")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await expect(history).to_contain_text("The captured check failed. I'll inspect its configuration.")
        await expect(history).to_contain_text("Use the captured data only.")
        await view.capture(name=f"{capture_name}_collapsed")

        group = history.get_by_role("button", name=re.compile(r"^2 tool calls, 1 reasoning step, 1 attachment"))
        await expect(group).to_contain_text("Failed")
        await group.click()
        tools = history.locator('[data-activity-step="tool"]')
        await expect(tools).to_have_count(3)
        read = tools.filter(has_text="README.md")
        failed = tools.filter(has_text="check-fixture")
        await expect(read.get_by_role("button", name=re.compile("^Read"))).to_have_attribute("aria-expanded", "false")
        await view.capture(name=f"{capture_name}_steps")

        await read.get_by_role("button", name=re.compile("^Read")).click()
        await read.get_by_role("region", name="Output", exact=True).scroll_into_view_if_needed()
        await expect(read).to_contain_text("The fixture README documents the required setting.")
        await failed.get_by_role("button", name=re.compile("^Bash")).click()
        await failed.get_by_role("region", name="Output", exact=True).scroll_into_view_if_needed()
        await expect(failed).to_contain_text("The captured test could not find the required setting.")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await view.capture(name=f"{capture_name}_tool_results")

        await failed.hover()
        await failed.get_by_role("button", name="Raw JSONL record", exact=True).click()
        raw = failed.get_by_role("region", name="Raw JSONL record")
        await expect(raw).to_have_count(2)
        await raw.last.scroll_into_view_if_needed()
        await expect(failed).to_contain_text('"tool_use_id": "run-check"')
        await view.capture(name=f"{capture_name}_sources")


if __name__ == "__main__":
    pytest_bazel.main()
