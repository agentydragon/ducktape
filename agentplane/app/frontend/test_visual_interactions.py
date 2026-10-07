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
from playwright.async_api import FloatRect, Locator, Page, Playwright, async_playwright, expect

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
    output_name: str | None = None,
) -> None:
    scenario = scenes[name]
    if output_name is not None:
        scenario = scenario.model_copy(update={"output_name": output_name})
    await capture_scenario(
        playwright_driver, name, scenario, config=sweep_config, output_dir=undeclared_outputs_dir(), drive=drive
    )


async def _in_viewport(target: Locator) -> None:
    await expect(target).to_be_visible()
    await expect(target).to_be_in_viewport(ratio=1)


async def _focus(page: Page, target: Locator) -> None:
    await target.evaluate("element => element.scrollIntoView({ block: 'center', inline: 'nearest' })")
    await wait_for_stable(page)
    await _in_viewport(target)


async def _open_raw_switches(page: Page) -> None:
    switches = page.locator("label").filter(has_text=re.compile(r"^Raw$"))
    await expect(switches.first).to_be_visible()
    count = await switches.count()
    assert count > 0
    for index in range(count):
        await switches.nth(index).click()
    await expect(page.locator('input[type="checkbox"]:checked').first).to_be_attached()


async def _select_reconnect(page: Page) -> None:
    connection = page.locator('select[name="connection"]')
    await connection.select_option(index=1)
    await page.locator('select[name="service_account"]').select_option(label="agentplane-visual/operator-assistant")
    await expect(page.locator("[data-reconnect-review]")).to_be_visible()


async def _open_run(page: Page) -> None:
    run = (
        page.locator(".agentplane-disclosure-summary[aria-expanded='false']")
        .filter(has_text=re.compile("tool call", re.IGNORECASE))
        .first
    )
    await run.click()
    await expect(page.locator(".agentplane-step-details").first).to_be_visible()


async def _open_recovery_details(page: Page) -> None:
    folded_run = page.locator(".agentplane-disclosure-summary[aria-expanded='false']").filter(
        has_text=re.compile("tool call", re.IGNORECASE)
    )
    if await folded_run.count():
        await _open_run(page)
    discarded = page.locator(".agentplane-disclosure-summary[aria-expanded='false']").filter(
        has_text="not retained in model context"
    )
    while await discarded.count():
        await discarded.first.click()
    steps = page.locator(".agentplane-step-details").filter(
        has_not=page.locator(".agentplane-step-title:text-is('Reasoning')")
    )
    await expect(steps.locator('[aria-busy="true"]')).to_have_count(0)
    count = await steps.count()
    for index in range(count):
        control = steps.nth(index).locator(".agentplane-disclosure-summary").first
        if await control.get_attribute("aria-expanded") == "false":
            await control.click()
    await wait_for_stable(page)


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


@pytest.mark.parametrize(
    "scene",
    ["session_recovery_messages_open", "session_recovery_messages_open_phone", "session_recovery_quiet_open"],
    ids=["messages-desktop", "messages-phone", "quiet-tools"],
)
async def test_recovery_details_open(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_recovery_details(page)
        if scene == "session_recovery_quiet_open":
            await expect(page.locator(".agentplane-step-details .agentplane-code-block").first).to_be_visible()
        else:
            await expect(page.locator('[aria-label="Not retained in context"]')).to_be_visible()
            await expect(page.locator('[aria-label="Retention unknown"]')).to_be_visible()
            await expect(page.locator(".agentplane-disclosure-summary[aria-expanded='true']").first).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def _open_debug_history(page: Page) -> None:
    await page.get_by_role("button", name="More", exact=True).click()
    await page.get_by_role("menuitem", name="Debug history").click()
    await expect(page.locator('[aria-label="Chronological observations"]')).to_be_visible()


@pytest.mark.parametrize(
    "scene",
    [
        "session_error_raw",
        "session_error_raw_phone",
        "session_interleaved_raw",
        "session_interleaved_raw_phone",
        "session_raw",
        "session_raw_phone",
        "session_pending_raw",
    ],
)
async def test_debug_history_latest(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_debug_history(page)
        await expect(
            page.locator('[aria-label="Chronological observations"] [data-debug-observation]').first
        ).to_be_visible()
        if scene == "session_pending_raw":
            await expect(page.locator('[data-thread-anchor="16"]')).to_be_visible()
            await expect(page.locator('.agentplane-user-bubble[data-message-phase="local"]')).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_debug_history_stderr_disclosure(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_debug_history(page)
        stderr = page.locator('[data-debug-observation="31"] .agentplane-disclosure-summary')
        await stderr.click()
        await expect(stderr).to_have_attribute("aria-expanded", "true")

    await _capture(
        "session_interleaved_disclosures",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_thread_setup_output(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        setup = page.locator(".agentplane-disclosure-summary").filter(has_text="Thread setup complete")
        await setup.click()
        await expect(setup).to_have_attribute("aria-expanded", "true")
        await expect(page.get_by_text("Setup stdout")).to_be_visible()

    await _capture(
        "session_thread_setup_open",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_archived_thread_toggle(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        # Mantine's visible track sits over the input; click the switch's label.
        await page.locator(".agentplane-sidebar-archived-toggle label").click()
        await expect(page.get_by_role("switch", name="Show archived threads")).to_be_checked()
        await expect(page.locator('.agentplane-thread-status-indicator[data-status="archived"]')).to_be_visible()
        await page.mouse.move(0, 0)
        await expect(page.get_by_role("tooltip")).to_have_count(0)

    await _capture(
        "threads_archived", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


@pytest.mark.parametrize(
    ("scene", "after_open"),
    [
        ("threads_phone_drawer", "a.agentplane-sidebar-group-name"),
        ("threads_failed_turn_phone_drawer", '.agentplane-thread-status-indicator[data-status="turn_error"]'),
        ("threads_provisioning_phone", 'a[href="#/sandboxes/test-provisioning"]'),
        ("threads_disconnected_phone", '[data-connection="degraded"]'),
    ],
    ids=["normal", "failed-turn", "provisioning", "disconnected"],
)
async def test_mobile_navigation_drawer(
    scene: str, after_open: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await page.get_by_role("button", name="Toggle navigation").click()
        await expect(page.locator(".agentplane-sidebar-open")).to_be_visible()
        await expect(page.locator(after_open).first).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_disconnected_threads_tooltip(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        indicator = page.locator('[data-connection][aria-label*="reconnecting"]')
        await indicator.focus()
        await expect(page.get_by_text(re.compile("Threads: reconnecting since"))).to_be_visible()

    await _capture(
        "threads_disconnected", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


@pytest.mark.parametrize("scene", ["sandboxes_claude_paused", "sandbox_claude_paused"], ids=["list", "detail"])
async def test_paused_claude_harness_choice(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        harness = page.get_by_role("combobox", name="Harness")
        await expect(harness).to_have_value("Codex")
        await harness.click()
        await expect(page.locator('[role="option"][data-combobox-disabled]')).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def _assert_phone_composer_layout(page: Page) -> None:
    send = page.locator('.agentplane-composer-send button[aria-label="Send"]')
    model = page.locator(".agentplane-composer-model")
    effort = page.locator(".agentplane-composer-effort")
    await expect(page.locator(".agentplane-topbar-title .agentplane-thread-status-indicator")).to_be_visible()
    await expect(page.locator(".agentplane-composer-controls")).to_be_visible()
    send_box = await send.bounding_box()
    model_box = await model.bounding_box()
    effort_box = await effort.bounding_box()
    viewport = page.viewport_size
    assert send_box is not None
    assert model_box is not None
    assert effort_box is not None
    assert viewport is not None
    assert send_box["width"] > 0
    assert send_box["x"] >= 0
    assert send_box["x"] + send_box["width"] <= viewport["width"]
    assert model_box["width"] > 0
    assert effort_box["width"] > 0
    assert abs(model_box["y"] - send_box["y"]) < 8
    assert abs(effort_box["y"] - send_box["y"]) < 8


@pytest.mark.parametrize(
    "scene",
    ["actions_attention_composer_open_desktop", "actions_attention_composer_open_phone"],
    ids=["desktop", "phone"],
)
async def test_inline_action_review(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await page.get_by_role("button", name="Review pending actions").click()
        await expect(page.get_by_role("button", name="Hide pending action details")).to_be_visible()
        await expect(page.locator(".action-affordance-notice .agentplane-code-block").first).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    "scene",
    ["actions_attention_composer_long_desktop", "actions_attention_composer_long_phone"],
    ids=["desktop", "phone"],
)
async def test_inline_action_review_scrolls_to_decisions(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await page.get_by_role("button", name="Review pending actions").click()
        details = page.locator(".action-affordance-details:not([hidden])")
        await expect(details.get_by_role("button", name="Approve").first).to_be_attached()
        await details.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        await wait_for_stable(page)
        assert await details.evaluate("element => element.scrollTop") > 0
        await _in_viewport(details.get_by_role("button", name="Approve").last)
        if scene.endswith("phone"):
            await _assert_phone_composer_layout(page)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_phone_composer_controls_fit(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    await _capture(
        "session_phone_controls",
        _assert_phone_composer_layout,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def test_actions_raw_switches(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_raw_switches(page)
        raw_switches = page.locator("label").filter(has_text=re.compile(r"^Raw$"))
        await _focus(page, raw_switches.last)

    await _capture("actions_raw", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["sandbox_status_raw", "sandbox_status_raw_phone"], ids=["desktop", "phone"])
async def test_sandbox_status_raw_switches(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_raw_switches(page)
        await expect(page.locator(".agentplane-code-block").first).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["connections", "connections_phone"], ids=["desktop", "phone"])
async def test_connections_settings_modal(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        if scene == "connections_phone":
            await page.get_by_role("button", name="Toggle navigation").click()
        await page.get_by_role("button", name="Settings").click()
        await expect(page.locator("[data-connection-id]").first).to_be_visible()
        if scene == "connections_phone":
            await page.mouse.move(0, 0)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_consent_reconnect_warning_desktop(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _select_reconnect(page)
        await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))

    await _capture(
        "consent_reconnect", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


@pytest.mark.parametrize("scene", ["actions_history", "actions_history_phone"], ids=["desktop", "phone"])
async def test_action_history_receipt(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        receipt = page.get_by_text("list the test backup archive", exact=True)
        await _focus(page, receipt)
        await _in_viewport(page.get_by_text("History", exact=True))

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["actions_history", "actions_history_phone"], ids=["desktop", "phone"])
async def test_action_history_diagram(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        image = page.locator('img[src^="data:image/"]').first
        await _focus(page, image)
        await _in_viewport(page.get_by_text("render the test diagram", exact=True))

    await _capture(
        scene,
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
        output_name=f"{scene}_diagram",
    )


async def test_action_history_raw_receipt(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        result = page.get_by_text("Result", exact=True).first
        await _open_raw_switches(page)
        await _focus(page, result)

    await _capture(
        "actions_history_raw", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


async def test_action_history_paging_control(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        load_more = page.get_by_test_id("action-history-load-more")
        await expect(load_more).to_have_text("Load more")
        await page.locator(".agentplane-shell-main").evaluate(
            "element => { element.scrollTop = element.scrollHeight; }"
        )
        await wait_for_stable(page)
        # The scroll extent can grow after the first move. Bring the control into view after
        # that layout settles, then verify the final captured state.
        await load_more.scroll_into_view_if_needed()
        await wait_for_stable(page)
        await _in_viewport(load_more)

    await _capture(
        "actions_history_more", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


@pytest.mark.parametrize("scene", ["mcp_servers", "mcp_servers_phone"], ids=["desktop", "phone"])
async def test_mcp_servers_linked_and_expired(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _in_viewport(page.locator('[data-mcp-server="linkage:example_docs"]').get_by_text("linked", exact=True))
        await _in_viewport(
            page.locator('[data-mcp-server="linkage:example_cluster"]').get_by_text("expired", exact=True)
        )

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    ("scene", "focus_key", "focus_state", "other_key", "other_state", "suffix"),
    [
        ("mcp_servers_phone", "linkage:example_pantry", "unlinked", "linkage:example_calendar", "degraded", "oauth"),
        ("mcp_servers", "group:example_notes", "available", "group:example_mail", "connect_failed", "health"),
        ("mcp_servers_phone", "group:example_notes", "available", "group:example_mail", "connect_failed", "health"),
    ],
    ids=["oauth-phone", "health-desktop", "health-phone"],
)
async def test_mcp_servers_lower_statuses(
    scene: str,
    focus_key: str,
    focus_state: str,
    other_key: str,
    other_state: str,
    suffix: str,
    scenes: dict[str, Scenario],
    playwright_driver: Playwright,
    sweep_config: SweepConfig,
) -> None:
    async def drive(page: Page) -> None:
        focused = page.locator(f'[data-mcp-server="{focus_key}"]').get_by_text(focus_state, exact=True)
        await _focus(page, focused)
        await _in_viewport(page.locator(f'[data-mcp-server="{other_key}"]').get_by_text(other_state, exact=True))
        if scene == "mcp_servers":
            # All lower states fit together at desktop width, so one focused image covers them.
            await _in_viewport(
                page.locator('[data-mcp-server="linkage:example_calendar"]').get_by_text("degraded", exact=True)
            )
            await _in_viewport(
                page.locator('[data-mcp-server="linkage:example_pantry"]').get_by_text("unlinked", exact=True)
            )

    await _capture(
        scene,
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
        output_name=f"{scene}_{suffix}",
    )


async def test_consent_reconnect_warning_phone(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _select_reconnect(page)
        await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))
        await _in_viewport(page.get_by_text(re.compile("I confirm replacing this Connection")))

    await _capture(
        "consent_reconnect_phone", drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config
    )


async def test_consent_reconnect_decision_phone(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _select_reconnect(page)
        authorize = page.get_by_role("button", name="Authorize")
        await _focus(page, authorize)
        await _in_viewport(page.get_by_role("button", name="Deny"))
        await expect(authorize).to_be_disabled()

    await _capture(
        "consent_reconnect_phone",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
        output_name="consent_reconnect_phone_decision",
    )


@pytest.mark.parametrize("scene", ["session_recovery_tools_open", "session_recovery_tools_open_phone"])
async def test_recovery_tool_lower_states(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_recovery_details(page)
        await _in_viewport(page.get_by_text("Failed, still in context", exact=True))
        await _in_viewport(page.locator('[aria-label="Retention unknown"]').last)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["session_recovery_tools_open", "session_recovery_tools_open_phone"])
async def test_revised_recovery_tool_output(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_recovery_details(page)
        output = page.locator(".agentplane-output-label").filter(has_text="Continuation output")
        await _focus(page, output)
        await _in_viewport(page.get_by_text("aborted", exact=True))

    await _capture(
        scene,
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
        output_name=f"{scene}_revised",
    )


@pytest.mark.parametrize("scene", ["session_more_menu", "session_more_menu_phone"], ids=["desktop", "phone"])
async def test_composer_more_menu(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await page.get_by_role("button", name="More", exact=True).click()
        await expect(page.get_by_role("menuitem", name="Debug history")).to_be_visible()
        await expect(page.get_by_role("menu")).to_be_visible()
        await expect(page.locator('[data-thread-anchor="34"]')).to_be_attached()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["session_reasoning", "session_reasoning_phone"], ids=["desktop", "phone"])
async def test_reasoning_inside_tool_run(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_run(page)
        reasoning = page.locator(".agentplane-step-details").filter(
            has=page.locator(".agentplane-step-title:text-is('Reasoning')")
        )
        await reasoning.locator(".agentplane-disclosure-summary").first.click()
        await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    "scene", ["session_reasoning_sticky", "session_reasoning_sticky_phone"], ids=["desktop", "phone"]
)
async def test_reasoning_heading_sticks_at_history_bottom(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
        await reasoning.locator(".agentplane-disclosure-summary").first.click()
        await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
        history = page.locator("[aria-label='Thread history']")
        await expect(history).to_have_attribute("data-layout-settled", "true")
        await history.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        await wait_for_stable(page)
        await expect(reasoning.locator(".agentplane-disclosure-summary").first).to_have_attribute(
            "aria-expanded", "true"
        )

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    "scene",
    [
        "session_standalone_reasoning_open",
        "session_reasoning_code_fence_open",
        "session_reasoning_code_fence_open_phone",
    ],
    ids=["standalone", "fence-desktop", "fence-phone"],
)
async def test_standalone_reasoning_opens(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
        summary = reasoning.locator(".agentplane-disclosure-summary")
        await summary.click()
        await expect(summary).to_have_attribute("aria-expanded", "true")
        await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
        if "code_fence" in scene:
            await expect(reasoning.locator(".agentplane-code-block .cm-content")).to_be_attached()
        else:
            await expect(reasoning.locator('a[href="https://example.test/projection"]')).to_have_text("projection path")

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize("scene", ["session_shell_calls", "session_shell_calls_phone"], ids=["desktop", "phone"])
async def test_shell_call_run_previews(
    scene: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        await _open_run(page)
        await expect(page.locator(".agentplane-step-details .agentplane-step-preview").first).to_be_visible()

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    ("scene", "anchor"), [("session_evidence", "4"), ("session_evidence_phone", "34")], ids=["desktop", "phone"]
)
async def test_thread_evidence_panel(
    scene: str, anchor: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        owner = page.locator(f'[data-thread-anchor="{anchor}"]')
        button = owner.locator("button.agentplane-evidence-toggle").first
        # The overlay button is reachable by keyboard even when another row covers its corner.
        await button.focus()
        await button.press("Enter")
        await expect(button).to_have_attribute("aria-expanded", "true")
        await expect(owner.locator("[data-evidence-observation]").first).to_be_visible()
        await button.blur()
        await page.mouse.move(0, 0)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


@pytest.mark.parametrize(
    ("scene", "target"),
    [
        ("session_evidence_hover_bubble", '[data-thread-anchor="4"] .agentplane-user-bubble'),
        ("session_evidence_hover_reply", '[data-thread-anchor="34"] .agentplane-evidence-owner'),
    ],
    ids=["bubble", "reply"],
)
async def test_evidence_button_reveals_on_hover(
    scene: str, target: str, scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        owner = page.locator(target)
        await owner.hover()
        await expect(owner.get_by_role("button", name="Evidence")).to_have_css("opacity", "1")

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


async def test_evidence_button_reveals_on_tap(
    scenes: dict[str, Scenario], playwright_driver: Playwright, sweep_config: SweepConfig
) -> None:
    async def drive(page: Page) -> None:
        owner = page.locator('[data-thread-anchor="34"] .agentplane-evidence-owner')
        await owner.tap()
        await expect(owner).to_have_attribute("data-evidence-revealed", "")

    await _capture(
        "session_evidence_tap_phone",
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
    )


async def _open_tool_run(page: Page) -> None:
    await _open_run(page)
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
    ("scene", "call_text", "tool", "suffix"),
    [
        ("session_shell_calls_open", "List every container and its status", "Bash", "claude"),
        ("session_shell_calls_open_phone", "List every container and its status", "Bash", "claude"),
        ("session_shell_calls_open", "Test Bank", "Shell", "codex"),
        ("session_shell_calls_open_phone", "Test Bank", "Shell", "codex"),
    ],
    ids=["claude-desktop", "claude-phone", "codex-desktop", "codex-phone"],
)
async def test_shell_call_command_and_output(
    scene: str,
    call_text: str,
    tool: str,
    suffix: str,
    scenes: dict[str, Scenario],
    playwright_driver: Playwright,
    sweep_config: SweepConfig,
) -> None:
    async def drive(page: Page) -> None:
        await _open_tool_run(page)
        call = page.locator(".agentplane-step-details").filter(has_text=call_text)
        await expect(call.locator(".agentplane-step-title")).to_have_text(tool)
        await _focus(page, call.locator(".agentplane-clamped-block[data-label='Command']"))
        await _in_viewport(call.locator(".agentplane-output-label"))

    await _capture(
        scene,
        drive,
        scenes=scenes,
        playwright_driver=playwright_driver,
        sweep_config=sweep_config,
        output_name=f"{scene}_{suffix}",
    )


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
        await expect(page.get_by_role("tooltip")).to_have_count(0)
        await wait_for_stable(page)

    await _capture(scene, drive, scenes=scenes, playwright_driver=playwright_driver, sweep_config=sweep_config)


if __name__ == "__main__":
    pytest_bazel.main()
