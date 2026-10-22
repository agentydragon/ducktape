"""Render fixture states with explicit Python readiness and capture."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize(
    "scene",
    [
        "DefinitionDetail",
        "FileViewerAnnotated",
        "FileViewerGroundTruth",
        "LLMRequests",
        "LLMRequestsToolCall",
        "DistributionChartRecall",
        "DistributionChartTP",
        "CoverageHeatmap",
        "OccurrenceStatsTable",
        "RunsBrowser",
        "RunDetailCritic",
        "SnapshotDetail",
    ],
)
async def test_fixture(visual: VisualHarness, scene: str, capture_name: str) -> None:
    async with visual.open(scene, viewport=Viewport(), capture_name=capture_name) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.capture(scene, target=view.page.locator("#shot"))


async def test_llm_request_json_disclosures(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open("LLMRequestsToolCall", viewport=Viewport(), capture_name=capture_name) as view:
        await expect(view.page.locator("#shot > div > details[open]")).to_have_count(1)
        for summary in await view.page.locator("#shot details summary").filter(has_text="Raw").all():
            await summary.click()
        await view.page.locator("#shot summary").filter(has_text="Request params").click()
        await view.page.locator("#shot summary").filter(has_text="Response details").click()
        await expect(view.page.locator("#shot .agentplane-code-block").last).to_be_visible()
        await view.capture("LLMRequestsToolCallJsonDisclosures", target=view.page.locator("#shot"))


async def test_llm_request_json_dark(visual: VisualHarness, capture_name: str) -> None:
    async with visual.open(
        "LLMRequestsToolCall", viewport=Viewport(), color_scheme="dark", capture_name=capture_name
    ) as view:
        await expect(view.page.locator("#shot .agentplane-code-block").first).to_be_visible()
        await view.capture("LLMRequestsToolCallDark", target=view.page.locator("#shot"))


if __name__ == "__main__":
    pytest_bazel.main()
