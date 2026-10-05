"""Capture every scenario of a `file://` harness with Playwright: one `test_scenario` per scenario.

The Python counterpart of `runScenarios` in `frontend_visual/visual-test-lib.mjs`, run by the
`py_visual_test` macro (`frontend_visual/py_visual_test.bzl`), which names this module its
`main_module` and sets the environment `SweepConfig` reads. The scenarios are the rows of a
`scenarios.json` (`visual_scenarios`); each is rendered, gated, and published as
`<outputName>-actual.png` (the suffix is the lane's choice) plus an entry in `visual-review.json`, for PR visual review
(`devinfra/pr_visuals`). There are no checked-in baselines: a scenario passes when it renders healthily.

Selection is pytest's, which is what Bazel drives: `--test_filter=<scenario>` is `-k` (a scenario's
name is its test id), and `shard_count` is `util.testing.sharding`, filter first, then shard.
A scenario that fails fails its own test and the sweep goes on, so one run enumerates every broken scene.

Deviation from the Puppeteer sweep: each scenario gets a fresh browser (about 0.1s on the RBE worker,
against ~10s of rendering per shard) rather than a page of one shared per shard. Viewport, device scale
factor, touch and colour scheme are then context options as Playwright intends, and what one scenario
renders cannot depend on which others ran before it or on which shard it landed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import Playwright, async_playwright

from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import FROZEN_NOW_MS, deterministic_browser_context
from util.testing.page_capture import (
    WAIT_TIMEOUT_MS,
    PageErrors,
    RequestFence,
    assert_network_settled,
    screenshot_element,
    wait_for_stable,
)
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_review import upsert_review_asset
from util.testing.visual_scenarios import Scenario, load_scenarios
from util.visual_review import VisualReviewAsset

# One event loop for the whole sweep, so one Playwright driver serves every scenario.
pytestmark = pytest.mark.asyncio(loop_scope="session")

# `document.fonts.check` is true for a family no `@font-face` declares, so on its own it passes when the
# stylesheet declaring the font never arrived (or the name is misspelled) and the page renders in a fallback.
# A face loads only once laid-out text uses it, so this is asked of a mounted, painted scene, after
# `document.fonts.ready` has covered a load that frame started.
_FONT_STATUS_JS = """async family => {
    await document.fonts.ready;
    const declared = Array.from(document.fonts).some((face) => face.family.replace(/^["']|["']$/g, "") === family);
    if (!declared) return "undeclared";
    return document.fonts.check(`16px "${family}"`) ? "loaded" : "unloaded";
}"""


@dataclass(frozen=True)
class SweepConfig:
    harness_path: Path
    scenarios_path: Path
    title: str
    expected_font_family: str | None
    output_suffix: str

    @classmethod
    def from_env(cls) -> SweepConfig:
        """What `py_visual_test` sets: runfiles paths (`rlocationpath`) of the bundle and the table, and the rest as is."""
        return cls(
            harness_path=get_required_path(os.environ["HARNESS_PATH"]),
            scenarios_path=get_required_path(os.environ["SCENARIOS_PATH"]),
            title=os.environ["VISUAL_TITLE"],
            expected_font_family=os.environ.get("EXPECTED_FONT_FAMILY"),
            output_suffix=os.environ.get("OUTPUT_SUFFIX", "-actual"),
        )

    @property
    def harness_url(self) -> str:
        """The harness page: `index.html` beside the bundle, or beside the `dist/` directory holding it."""
        directory = self.harness_path.parent
        index = (directory.parent if directory.name == "dist" else directory) / "index.html"
        if not index.exists():
            raise FileNotFoundError(f"harness index.html not found for {self.harness_path}")
        # absolute(), not resolve(): the page pulls its bundle by relative path, which only exists beside
        # the runfiles symlink, not beside whatever it points to.
        return index.absolute().as_uri()


async def capture_scenario(
    playwright: Playwright,
    scenario_name: str,
    scenario: Scenario,
    *,
    config: SweepConfig,
    output_dir: Path,
    timeout_ms: int = WAIT_TIMEOUT_MS,
) -> None:
    """Render one scenario on its own browser; raise, naming it, if it is not healthy."""
    output_name = scenario.output_name or scenario_name
    async with await deterministic_browser_context(
        playwright,
        viewport={"width": scenario.viewport.width, "height": scenario.viewport.height},
        frozen_now_ms=FROZEN_NOW_MS,
        color_scheme=scenario.color_scheme,
        device_scale_factor=scenario.viewport.device_scale_factor,
        has_touch=scenario.viewport.has_touch,
        # The harness page is a file:// URL, so it needs file access to reach its own bundle.
        extra_args=["--allow-file-access-from-files"],
    ) as context:
        page = await context.new_page()
        page_errors = PageErrors(page)
        # The harness is entirely local (file:// page, bundled fixtures), so nothing may reach the network.
        fence = RequestFence(lambda request: request.url.startswith("file://"))
        await fence.install(page)

        query = {"page": scenario_name} if scenario.query is None else scenario.query
        await page.goto(f"{config.harness_url}?{urlencode(query)}", wait_until="networkidle", timeout=timeout_ms)
        await page.wait_for_selector("#app > *", state="attached", timeout=timeout_ms)
        for selector in scenario.ready_selectors:
            await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        # Last, so fonts, images and paint settle around whatever the scene's own conditions let in.
        await wait_for_stable(page)
        # Only assert a named font when the app declares one. Generic family resolution is owned by the
        # deterministic browser profile, and must not be emulated with test CSS.
        if config.expected_font_family:
            font_status = await page.evaluate(_FONT_STATUS_JS, config.expected_font_family)
            if font_status != "loaded":
                raise AssertionError(f"{output_name}: {config.expected_font_family} font did not load ({font_status})")
        # A pointer state no page script can make: :hover and a real touch. Settled again for what it shows.
        if scenario.hover:
            await page.hover(scenario.hover, timeout=timeout_ms)
        if scenario.tap:
            await page.tap(scenario.tap, timeout=timeout_ms)
        if scenario.hover or scenario.tap:
            await wait_for_stable(page)
        await assert_network_settled(page, context=output_name, timeout_ms=timeout_ms)
        fence.assert_none_escaped(context=output_name)
        page_errors.assert_none(context=output_name)

        # Viewport captures preserve clipping instead of expanding to fit an overflowing app.
        screenshot = (
            await page.screenshot()
            if scenario.capture_viewport
            else await screenshot_element(page, scenario.element, context=output_name)
        )

    asset = VisualReviewAsset(path=f"{output_name}{config.output_suffix}.png", label=scenario.label or output_name)
    (output_dir / asset.path).write_bytes(screenshot)
    upsert_review_asset(output_dir, title=config.title, asset=asset)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "scenario_name" not in metafunc.fixturenames:
        return
    if not (scenarios := load_scenarios(SweepConfig.from_env().scenarios_path)):
        # An empty parameter set is skipped, which Bazel reports as a pass.
        raise ValueError("the scenario table is empty")
    metafunc.parametrize(("scenario_name", "scenario"), scenarios.items(), ids=list(scenarios))


@pytest.fixture(scope="session")
def sweep_config() -> SweepConfig:
    return SweepConfig.from_env()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def playwright_driver() -> AsyncIterator[Playwright]:
    async with async_playwright() as playwright:
        yield playwright


async def test_scenario(
    scenario_name: str, scenario: Scenario, playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    await capture_scenario(
        playwright_driver, scenario_name, scenario, config=sweep_config, output_dir=undeclared_outputs_dir()
    )


def main() -> None:
    # This file is the test module as well as the entry point, so pytest is pointed at it by path:
    # its name is not one pytest collects on its own.
    pytest_bazel.main([*sys.argv[1:], __file__])


if __name__ == "__main__":
    main()
