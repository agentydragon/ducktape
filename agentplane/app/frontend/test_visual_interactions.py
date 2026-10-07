"""Playwright interactions for Agentplane's canned visual fixtures.

The TypeScript harness chooses fixture data by scene name. Each test below states the action and
assertion for a real UI behavior, then uses the shared visual capture and review manifest writer.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import FloatRect, Page, Playwright, async_playwright, expect

from util.bazel.runfiles import get_required_path
from util.testing import visual_sweep
from util.testing.page_capture import wait_for_stable
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_scenarios import Scenario, load_scenarios
from util.testing.visual_sweep import SweepConfig, capture_scenario

# The shared Playwright driver is session-scoped; tests must run on its event loop, as the generic
# visual sweep does. Asyncio auto mode handles discovery; this mark only aligns the loop scope.
pytestmark = pytest.mark.asyncio(loop_scope="session")
pytest_generate_tests = visual_sweep.pytest_generate_tests

_SCROLL = "[data-disclosure-demo-scroll]"
_HEADING = ".agentplane-disclosure-heading"
_MAIN = f".demo-main .agentplane-disclosure-item > {_HEADING}"
_OUTER = f".demo-outer > .agentplane-disclosure-item > {_HEADING}"
_INNER = f".demo-inner > .agentplane-disclosure-item > {_HEADING}"
_OUTPUT = f".demo-output > .agentplane-disclosure-item > {_HEADING}"


@pytest.fixture(scope="session")
def sweep_config() -> SweepConfig:
    return SweepConfig.from_env()


@pytest.fixture(scope="session")
def scenes() -> dict[str, Scenario]:
    return load_scenarios(get_required_path(os.environ["INTERACTION_SCENARIOS_PATH"]))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def playwright_driver() -> AsyncIterator[Playwright]:
    async with async_playwright() as playwright:
        yield playwright


async def test_scenario(
    scenario_name: str, scenario: Scenario, playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    await visual_sweep.test_scenario(scenario_name, scenario, playwright_driver, sweep_config)


async def _capture(
    name: str,
    drive: Callable[[Page], Awaitable[None]],
    *,
    scenes: dict[str, Scenario],
    playwright_driver: Playwright,
    sweep_config: SweepConfig,
) -> None:
    await capture_scenario(
        playwright_driver, name, scenes[name], config=sweep_config, output_dir=undeclared_outputs_dir(), drive=drive
    )


async def _box(page: Page, selector: str) -> FloatRect:
    box = await page.locator(selector).bounding_box()
    assert box is not None, f"missing box for {selector}"
    return box


async def _height(page: Page, selector: str) -> float:
    return (await _box(page, selector))["height"]


async def _scroll_to(page: Page, selector: str, screen_top: float) -> None:
    await page.locator(selector).evaluate(
        """(element, screenTop) => {
            const viewport = element.closest('[data-disclosure-demo-scroll]');
            if (!viewport) throw new Error('disclosure scroll viewport is missing');
            viewport.scrollTop = 0;
            const top = element.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
            viewport.scrollTop = Math.max(0, top - screenTop);
        }""",
        screen_top,
    )
    await wait_for_stable(page)
    assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") > 0


async def _scroll_to_copy(page: Page, target: str, screen_top: float) -> None:
    await _scroll_to(page, f'[data-demo-target="{target}"]', screen_top)


async def _heading_top(page: Page, selector: str) -> float:
    return (await _box(page, selector))["y"] - (await _box(page, _SCROLL))["y"]


async def _expect_at(page: Page, selector: str, offset: float) -> None:
    actual = await _heading_top(page, selector)
    assert abs(actual - offset) <= 4, f"{selector} at {actual}px; expected {offset}px"


async def _expect_released(page: Page, selector: str) -> None:
    box = await _box(page, selector)
    viewport = await _box(page, _SCROLL)
    assert box["y"] + box["height"] <= viewport["y"] + 1, f"{selector} remained visible past its block"


async def test_collapsed_disclosure(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await expect(page.locator(_MAIN)).to_have_attribute("data-expanded", "false")
        assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") == 0

    await _capture(
        "disclosure_component_phone_collapsed",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_short_disclosure_fits(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        assert not await page.locator(_SCROLL).evaluate("element => element.scrollHeight > element.clientHeight")
        await expect(page.locator(_MAIN)).to_have_attribute("data-expanded", "true")

    await _capture(
        "disclosure_component_phone_short_expanded",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_long_disclosure_before_sticking(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        assert await page.locator(_SCROLL).evaluate("element => element.scrollHeight > element.clientHeight")
        assert await page.locator(_SCROLL).evaluate("element => element.scrollTop") == 0
        top = await _heading_top(page, _MAIN)
        viewport = await _box(page, _SCROLL)
        assert 0 < top < viewport["height"] * 0.6

    await _capture(
        "disclosure_component_phone_long_top",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_long_disclosure_sticks(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _scroll_to_copy(page, "long-paragraph", 64)
        await _expect_at(page, _MAIN, 0)

    await _capture(
        "disclosure_component_phone_long_scrolled",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_disclosure_releases_after_content(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _scroll_to_copy(page, "following-disclosure", 64)
        await _expect_released(page, _MAIN)

    await _capture(
        "disclosure_component_phone_after_disclosure",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_nested_parent_sticks_before_child(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height = await _height(page, _OUTER)
        await _scroll_to_copy(page, "outer-paragraph", outer_height + 8)
        await _expect_at(page, _OUTER, 0)
        inner_top = await _heading_top(page, _INNER)
        viewport = await _box(page, _SCROLL)
        assert outer_height + 4 < inner_top < viewport["height"]

    await _capture(
        "disclosure_component_phone_nested_parent_only",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


@pytest.mark.parametrize(
    ("scene", "wrapped"),
    [
        ("disclosure_component_phone_nested_child_scrolled", False),
        ("disclosure_component_phone_nested_wrapped_headings", True),
    ],
    ids=["plain", "wrapped"],
)
async def test_nested_headings_stack(
    scene: str, wrapped: bool, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height = await _height(page, _OUTER)
        inner_height = await _height(page, _INNER)
        await _scroll_to_copy(page, "nested-paragraph", outer_height + inner_height + 16)
        await _expect_at(page, _OUTER, 0)
        await _expect_at(page, _INNER, outer_height)
        if wrapped:
            assert outer_height >= 56, "outer mobile heading did not wrap"
            assert inner_height >= 56, "inner mobile heading did not wrap"

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_nested_child_releases_behind_parent(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height = await _height(page, _OUTER)
        await _scroll_to_copy(page, "following-nested", outer_height + 16)
        await _expect_at(page, _OUTER, 0)
        outer_box, inner_box = await _box(page, _OUTER), await _box(page, _INNER)
        outer_z = int(await page.locator(_OUTER).evaluate("element => getComputedStyle(element).zIndex"))
        inner_z = int(await page.locator(_INNER).evaluate("element => getComputedStyle(element).zIndex"))
        assert inner_box["y"] + inner_box["height"] <= outer_box["y"] + outer_box["height"] + 1
        assert outer_z > inner_z

    await _capture(
        "disclosure_component_phone_nested_after_child",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_nested_parent_releases_after_content(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _scroll_to_copy(page, "following-outer", 64)
        await _expect_released(page, _INNER)
        await _expect_released(page, _OUTER)

    await _capture(
        "disclosure_component_phone_nested_after_outer",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_output_heading_stacks_below_tool(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height, inner_height, output_height = (
            await _height(page, _OUTER),
            await _height(page, _INNER),
            await _height(page, _OUTPUT),
        )
        await _scroll_to_copy(page, "output-paragraph", outer_height + inner_height + output_height + 16)
        await _expect_at(page, _OUTER, 0)
        await _expect_at(page, _INNER, outer_height)
        await _expect_at(page, _OUTPUT, outer_height + inner_height)

    await _capture(
        "disclosure_component_phone_nested_expanded_output",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_output_heading_enters_below_tool(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height, inner_height = await _height(page, _OUTER), await _height(page, _INNER)
        await _scroll_to_copy(page, "before-output", outer_height + inner_height + 16)
        await _expect_at(page, _OUTER, 0)
        await _expect_at(page, _INNER, outer_height)
        output_top = await _heading_top(page, _OUTPUT)
        viewport = await _box(page, _SCROLL)
        assert outer_height + inner_height + 4 < output_top < viewport["height"]

    await _capture(
        "disclosure_component_phone_nested_before_output",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_collapsed_output_keeps_its_sticky_slot(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height, inner_height = await _height(page, _OUTER), await _height(page, _INNER)
        await _scroll_to(page, _OUTPUT, outer_height + inner_height)
        await _expect_at(page, _OUTER, 0)
        await _expect_at(page, _INNER, outer_height)
        await _expect_at(page, _OUTPUT, outer_height + inner_height)
        await expect(page.locator(_OUTPUT)).to_have_attribute("data-expanded", "false")

    await _capture(
        "disclosure_component_phone_nested_output_collapsed",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_output_heading_releases_after_content(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        outer_height, inner_height = await _height(page, _OUTER), await _height(page, _INNER)
        await _scroll_to_copy(page, "following-output", outer_height + inner_height + 16)
        await _expect_at(page, _OUTER, 0)
        await _expect_at(page, _INNER, outer_height)
        await _expect_released(page, _OUTPUT)

    await _capture(
        "disclosure_component_phone_nested_after_output",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def _open_tool_run(page: Page) -> None:
    run = (
        page.locator(".agentplane-disclosure-summary[aria-expanded='false']")
        .filter(has_text=re.compile("tool call", re.IGNORECASE))
        .first
    )
    await run.click()
    steps = page.locator(".agentplane-step-details")
    await expect(steps.first).to_be_visible()
    # Wait for the opened run's payloads to render before counting its tool lines. A count sampled
    # while an OptionalPayload is loading can miss a later call and photograph a folded output.
    await expect(steps.locator('[aria-busy="true"]')).to_have_count(0)
    await expect(page.locator("[aria-label='Thread history'][data-layout-settled='true']")).to_be_attached()
    # Open each tool-call line once; leave reasoning folded.
    tool_steps = steps.filter(has_not=page.locator(".agentplane-step-title:text-is('Reasoning')"))
    count = await tool_steps.count()
    assert count > 0, "tool run contained no call lines"
    for index in range(count):
        control = tool_steps.nth(index).locator(".agentplane-disclosure-summary").first
        if await control.get_attribute("aria-expanded") == "false":
            await control.click()
    await expect(page.locator(".agentplane-step-details .agentplane-code-block .cm-content").first).to_be_attached()
    await wait_for_stable(page)


@pytest.mark.parametrize(
    ("scene", "shell_calls"),
    [
        ("session_tool_payloads", False),
        ("session_tool_payloads_phone", False),
        ("session_shell_calls_open", True),
        ("session_shell_calls_open_phone", True),
    ],
    ids=["tool-desktop", "tool-phone", "shell-desktop", "shell-phone"],
)
async def test_open_tool_calls_and_output(
    scene: str, shell_calls: bool, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_tool_run(page)
        await expect(
            page.locator(".agentplane-output-disclosure .agentplane-disclosure-summary[aria-expanded='true']").first
        ).to_be_attached()
        if shell_calls:
            await expect(page.locator("[data-clamped='true']").first).to_be_attached()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    "scene", ["session_tool_output_sticky", "session_tool_output_sticky_phone"], ids=["desktop", "phone"]
)
async def test_expanded_shell_output_sticks_while_scrolling(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_tool_run(page)
        output = page.locator(".agentplane-output-disclosure .agentplane-disclosure-summary[aria-expanded='true']")
        await expect(output.first).to_be_attached()
        # The long Codex command and output are present before enumerating expansion controls.
        await expect(page.locator(".agentplane-clamped-block[data-label='Command']").first).to_be_attached()
        await expect(page.locator(".agentplane-clamped-block[data-label='Arguments']").first).to_be_attached()
        await expect(page.locator(".agentplane-clamped-block[data-label='Output']").first).to_be_attached()
        await expect(page.locator(".agentplane-step-details [aria-busy='true']")).to_have_count(0)
        await expect(page.locator("[aria-label='Thread history'][data-layout-settled='true']")).to_be_attached()
        controls = page.locator(".agentplane-clamped-block button[aria-expanded='false']")
        count = await controls.count()
        assert count > 0, "long command/output had no expansion controls"
        for _ in range(count):
            await controls.first.click()
            await wait_for_stable(page)
        await expect(controls).to_have_count(0)
        await expect(page.locator("[aria-label='Thread history'][data-layout-settled='true']")).to_be_attached()
        await page.locator("[aria-label='Thread history']").evaluate(
            "element => { element.scrollTop = element.scrollHeight; }"
        )
        await wait_for_stable(page)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def _open_select(
    page: Page, *, label: str, available: str, picked: str | None = None, press_arrow_down: bool = False
) -> None:
    selector = page.get_by_role("combobox", name=label, exact=True)
    await expect(selector).to_be_enabled()
    if press_arrow_down:
        await selector.press("ArrowDown")
    else:
        # Mantine opens this read-only combobox from its visible PillsInput wrapper.
        wrapper = selector.locator(
            "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' mantine-MultiSelect-input ')][1]"
        )
        await wrapper.click()
    await expect(page.get_by_role("option", name=re.compile(re.escape(available)))).to_be_visible()
    if picked is not None:
        await expect(page.get_by_role("option", name=re.compile(re.escape(picked)))).to_have_count(0)
    await page.mouse.move(0, 0)
    await wait_for_stable(page)


@pytest.mark.parametrize("scene", ["new_sandbox", "new_sandbox_phone"], ids=["desktop", "phone"])
async def test_action_policy_selector_hides_picked_option(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_select(page, label="Action policy sets", available="harness-reviews", picked="public-coder")

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["new_sandbox_policies", "new_sandbox_policies_phone"], ids=["desktop", "phone"])
async def test_egress_policy_selector_hides_picked_option(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_select(
            page, label="Egress policies", available="pypi", picked="github-public", press_arrow_down=True
        )

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["new_sandbox_grants", "new_sandbox_grants_phone"], ids=["desktop", "phone"])
async def test_grant_selector_hides_picked_option(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_select(page, label="Kubernetes grants", available="config-read", picked="workspace-read")

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["sandbox_egress", "sandbox_egress_phone"], ids=["desktop", "phone"])
async def test_sandbox_egress_pick_updates_options(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_select(page, label="Grant egress policies", available="pypi")
        await page.get_by_role("option", name=re.compile("pypi")).click()
        await expect(page.locator(".mantine-Pill-root", has_text="pypi")).to_be_visible()
        await expect(page.get_by_role("option", name=re.compile("github-public"))).to_be_visible()
        await expect(page.get_by_role("option", name=re.compile("pypi"))).to_have_count(0)
        await page.mouse.move(0, 0)
        await wait_for_stable(page)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


if __name__ == "__main__":
    pytest_bazel.main()
