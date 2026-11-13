"""Render fixture states with explicit Python readiness and capture."""

import re

import pytest
import pytest_bazel
from playwright.async_api import Locator, expect

from util.testing.frontend_visual.capture import VisualHarness
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _open_json_disclosures(request: Locator) -> None:
    for summary in await request.locator("details summary").filter(has_text="Raw").all():
        await summary.click()
    await request.locator("summary").filter(has_text="Request params").click()
    await request.locator("summary").filter(has_text="Response details").click()
    # Pick the last block by its `JsonCode` wrapper: the response details editor mounts a frame after
    # its disclosure opens, and until then `.ui-code-block` resolves to the Raw editor above it, whose
    # own `overflow-auto` box the scroll would then shift by its padding.
    last_json = request.locator(".props-json-code").last
    await last_json.scroll_into_view_if_needed()
    await expect(last_json.locator(".cm-content")).to_be_visible()


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
async def test_fixture(visual: VisualHarness, scene: str) -> None:
    async with visual.open(query={"page": scene}, viewport=Viewport()) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.capture(scene, target=view.page.locator("#shot"))


async def test_llm_request_json_disclosures(visual: VisualHarness) -> None:
    async with visual.open(query={"page": "LLMRequestsToolCall"}, viewport=Viewport()) as view:
        request = view.page.locator("#shot > div > details[open]")
        await expect(request).to_have_count(1)
        await _open_json_disclosures(request)
        await view.capture("LLMRequestsToolCallJsonDisclosures", target=view.page.locator("#shot"))


async def test_llm_request_json_dark(visual: VisualHarness) -> None:
    async with visual.open(query={"page": "LLMRequestsToolCall"}, viewport=Viewport(), color_scheme="dark") as view:
        request = view.page.locator("#shot > div > details[open]")
        await expect(request).to_have_count(1)
        await _open_json_disclosures(request)
        await view.capture("LLMRequestsToolCallDark", target=view.page.locator("#shot"))


async def test_llm_request_chat_completions(visual: VisualHarness) -> None:
    async with visual.open(query={"page": "LLMRequestsChatCompletions"}, viewport=Viewport()) as view:
        shot = view.page.locator("#shot")
        await expect(shot.locator("details[open]")).to_have_count(2)
        await expect(shot.get_by_text("Chat Request JSON")).to_have_count(2)
        await expect(shot.get_by_text("Chat Response JSON")).to_be_visible()
        await expect(shot.get_by_text("Error Response", exact=True)).to_be_visible()
        await expect(shot.locator(".ui-code-block .cm-content")).to_have_count(4)
        await view.capture("LLMRequestsChatCompletions", target=shot)


async def test_llm_request_anthropic(visual: VisualHarness) -> None:
    async with visual.open(query={"page": "LLMRequestsAnthropic"}, viewport=Viewport()) as view:
        shot = view.page.locator("#shot")
        await expect(shot.locator("details[open]")).to_have_count(1)
        await expect(shot.get_by_text("Anthropic Request JSON")).to_be_visible()
        await expect(shot.get_by_text("Anthropic Response JSON")).to_be_visible()
        sections = shot.locator(".p-4.space-y-3")
        await expect(sections).to_have_count(2)
        await view.capture("LLMRequestsAnthropicRequest", target=sections.nth(0))
        await sections.nth(1).scroll_into_view_if_needed()
        code = shot.locator(".ui-code-block .cm-content")
        await expect(code).to_have_count(2)
        await view.capture("LLMRequestsAnthropicResponse", target=sections.nth(1))


async def test_snapshot_cluster_run_link(visual: VisualHarness) -> None:
    run_id = "12345678-90ab-cdef-1234-567890abcdef"
    async with visual.open(query={"page": "SnapshotDetailClusterRunLink"}, viewport=Viewport()) as view:
        shot = view.page.locator("#shot")
        await shot.get_by_role("tab", name=re.compile("Clusters")).click()
        await shot.get_by_role("button", name=re.compile("hashing-issues")).click()
        run_link = shot.get_by_role("link", name="12345678")
        await expect(run_link).to_have_attribute("href", f"#/runs/{run_id}")
        await expect(run_link).to_have_attribute("title", run_id)
        await expect(run_link).to_have_class(re.compile(r"font-mono.*underline"))
        await view.capture("SnapshotClusterRunLink", target=shot)


if __name__ == "__main__":
    pytest_bazel.main()
