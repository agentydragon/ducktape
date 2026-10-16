"""Render fixture states with explicit Python readiness and capture."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness

# gazelle:include_dep //util/testing:visual_fixtures
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


if __name__ == "__main__":
    pytest_bazel.main()
