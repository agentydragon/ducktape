"""Agentplane history visual behavior tests."""

import re

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.frontend.testing.app import IDLE_THREAD, RUNNING_THREAD, SUSPENDED_THREAD, AgentplaneFixture, Scene
from agentplane.app.frontend.testing.visual_assertions import (
    _expand_tool_steps,
    _focus,
    _in_viewport,
    _open_debug_history,
    _open_run,
    _open_standalone_reasoning,
    _open_tool_run,
    _rollout_run,
    _rollout_start,
    _tool_steps,
)
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE, MOBILE_ONLY
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, MOBILE_TOUCH, Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize(
    ("viewport", "color_scheme"),
    [(DESKTOP, "light"), (MOBILE, "light"), (DESKTOP, "dark")],
    ids=["desktop-light", "mobile-light", "desktop-dark"],
)
async def test_completed_rollout_overview(completed_rollout: VisualPage) -> None:
    await _rollout_start(completed_rollout)
    await completed_rollout.capture()


@pytest_asyncio.fixture(loop_scope="session")
async def reported_rollout(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    await app.build(Scene.REPORTED_ROLLOUT)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    return view


async def test_reported_rollout_overview(reported_rollout: VisualPage) -> None:
    await _rollout_start(reported_rollout)
    await reported_rollout.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_composer_autosuspend_countdown(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.AUTOSUSPEND_COUNTDOWN)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    autosuspend = view.page.locator(
        ".agentplane-composer-autosuspend:visible, .agentplane-composer-autosuspend-mobile:visible"
    )
    activity = view.page.locator(
        ".agentplane-composer-model-activity:visible, .agentplane-thread-tail-model-activity:visible"
    )
    await expect(autosuspend).to_be_visible()
    await expect(autosuspend).to_have_text("Sandbox autosuspends in ~12m")
    await expect(activity).to_be_visible()
    await expect(activity).to_contain_text("Model activity: 13m ago · Idle")
    await view.check(context="autosuspend composer ready")
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_composer_autosuspend_busy(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.AUTOSUSPEND_BUSY)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    hint = view.page.locator(
        ".agentplane-composer-autosuspend:visible, .agentplane-composer-autosuspend-mobile:visible"
    )
    await expect(hint).to_have_text("Sandbox in active use by Session running a turn · Details")
    await expect(hint.get_by_role("link", name="Session running a turn")).to_have_attribute(
        "href", f"#/threads/{RUNNING_THREAD}"
    )
    await view.capture()
    await hint.get_by_role("button", name="Show autosuspend blockers").click()
    details = view.page.get_by_role("dialog", name="Show autosuspend blockers")
    await expect(details).to_be_visible()
    await expect(details.get_by_role("link", name="Session running a turn", exact=True)).to_be_visible()
    await expect(details.get_by_role("link", name="Session running setup", exact=True)).to_be_visible()
    await expect(details).to_contain_text("Sandbox bootstrap running")
    await view.capture(name="blockers")


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_composer_autosuspend_starting(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.AUTOSUSPEND_STARTING)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    hint = view.page.locator(
        ".agentplane-composer-autosuspend:visible, .agentplane-composer-autosuspend-mobile:visible"
    )
    await expect(hint).to_have_text("Autosuspend waiting: Sandbox starting")
    await expect(view.page.get_by_text("Continue when the Sandbox is ready.", exact=True)).to_be_visible()
    await view.capture()


@pytest.mark.parametrize(
    ("viewport", "color_scheme"),
    [(DESKTOP, "light"), (MOBILE, "light"), (DESKTOP, "dark")],
    ids=["desktop-light", "mobile-light", "desktop-dark"],
)
@pytest.mark.parametrize("position", ["start", "end"])
async def test_completed_rollout_run(completed_rollout: VisualPage, position: str) -> None:
    await _rollout_run(completed_rollout, position)
    await completed_rollout.capture()


@pytest.mark.parametrize("position", ["start", "end"], ids=["start", "end"])
async def test_reported_rollout_run(reported_rollout: VisualPage, position: str) -> None:
    await _rollout_run(reported_rollout, position)
    await reported_rollout.capture()


@pytest.mark.parametrize(
    ("expanded_output", "viewport"),
    [(False, DESKTOP), (False, MOBILE), (True, DESKTOP), (True, MOBILE)],
    ids=["false-desktop", "false-mobile", "true-desktop", "true-mobile"],
)
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_realistic_rollout_call(completed_rollout: VisualPage, expanded_output: bool, viewport: Viewport) -> None:
    page = completed_rollout.page
    await _rollout_start(completed_rollout)
    await _open_run(page)
    call = (
        page.locator(".agentplane-run-steps .agentplane-step-details")
        .filter(has=page.locator(".agentplane-step-title:text-is('Shell')"))
        .nth(1)
    )
    await call.locator(".agentplane-disclosure-summary").first.click()
    output = call.locator(".agentplane-clamped-block[data-label='Output']")
    await expect(output).to_be_attached()
    heading = call.locator(
        ".agentplane-output-disclosure > .agentplane-disclosure-item > .agentplane-disclosure-heading"
    )
    heading_box = await heading.bounding_box()
    card_box = await call.locator("xpath=ancestor::*[@data-open][1]").bounding_box()
    assert heading_box is not None
    assert card_box is not None
    assert heading_box["x"] >= card_box["x"] - 1
    assert heading_box["x"] + heading_box["width"] <= card_box["x"] + card_box["width"] + 1
    if expanded_output:
        await _focus(page, output)
        await output.get_by_role("button", name=re.compile("^Show all")).click()
        await expect(output).to_have_attribute("data-expanded", "true")
        history = page.get_by_role("region", name="Thread history")
        run_steps = page.locator(".agentplane-run-steps").first
        run_anchor = await run_steps.evaluate("element => element.closest('[data-thread-anchor]').dataset.threadAnchor")
        run_card = history.locator(f'[data-thread-anchor="{run_anchor}"] > .agentplane-collapsible-card')
        run_heading = run_card.locator(
            ":scope > .agentplane-disclosure > .agentplane-disclosure-item > .agentplane-disclosure-heading"
        )
        call_heading = call.locator(":scope > .agentplane-disclosure-item > .agentplane-disclosure-heading")
        output_heading = call.locator(
            ".agentplane-output-disclosure > .agentplane-disclosure-item > .agentplane-disclosure-heading"
        )
        history_box = await history.bounding_box()
        run_box = await run_heading.bounding_box()
        call_box = await call_heading.bounding_box()
        output_box = await output_heading.bounding_box()
        assert history_box is not None
        assert run_box is not None
        assert call_box is not None
        assert output_box is not None
        output_stack_top = run_box["height"] + call_box["height"]
        await history.evaluate(
            "(element, delta) => { element.scrollTop += delta; }",
            output_box["y"] - history_box["y"] - output_stack_top + 12,
        )
        await wait_for_stable(page)
        history_box = await history.bounding_box()
        assert history_box is not None
        for heading, expected_top in (
            (run_heading, 0),
            (call_heading, run_box["height"]),
            (output_heading, output_stack_top),
        ):
            heading_box = await heading.bounding_box()
            assert heading_box is not None
            actual_top = heading_box["y"] - history_box["y"]
            assert abs(actual_top - expected_top) <= 4, (
                f"{await heading.inner_text()} at {actual_top}px; expected sticky slot at {expected_top}px"
            )
        geometry = [
            await heading.evaluate(
                """element => {
                    const bounds = element.getBoundingClientRect();
                    const control = element.querySelector('.agentplane-disclosure-summary');
                    const label = element.querySelector('.agentplane-disclosure-summary-content');
                    const chevron = element.querySelector('.mantine-Accordion-chevron');
                    const controlBounds = control.getBoundingClientRect();
                    const labelBounds = label.getBoundingClientRect();
                    return {
                        left: labelBounds.left,
                        right: chevron.getBoundingClientRect().right,
                        headingLeft: bounds.left,
                        headingRight: bounds.right,
                        headingHeight: bounds.height,
                        controlHeight: controlBounds.height,
                        labelCenterOffset: labelBounds.top + labelBounds.height / 2 - (bounds.top + bounds.height / 2),
                        size: getComputedStyle(label).fontSize
                    };
                }"""
            )
            for heading in (run_heading, call_heading, output_heading)
        ]
        assert len({item["size"] for item in geometry}) == 1, geometry
        assert max(item["right"] for item in geometry) - min(item["right"] for item in geometry) <= 1, geometry
        assert max(item["headingLeft"] for item in geometry) - min(item["headingLeft"] for item in geometry) <= 1, (
            geometry
        )
        assert max(item["headingRight"] for item in geometry) - min(item["headingRight"] for item in geometry) <= 1, (
            geometry
        )
        expected_header_height = 44 if viewport.width < 768 else 28
        assert max(item["headingHeight"] for item in geometry) - min(item["headingHeight"] for item in geometry) <= 1, (
            geometry
        )
        assert all(abs(item["headingHeight"] - expected_header_height) <= 1 for item in geometry), geometry
        assert all(abs(item["controlHeight"] - item["headingHeight"]) <= 1 for item in geometry), geometry
        assert all(abs(item["labelCenterOffset"]) <= 1 for item in geometry), geometry
        indents = [geometry[index + 1]["left"] - geometry[index]["left"] for index in range(2)]
        assert all(abs(indent - 5) <= 1 for indent in indents), indents
    else:
        await _focus(page, call.locator(".agentplane-clamped-block[data-label='Command']"))
    await page.mouse.move(0, 0)
    await completed_rollout.capture()
    if expanded_output:
        # The user can shorten output, close the call, and close the run from the
        # current reading position. Assert visibility before clicks can auto-scroll.
        show_less = output_heading.get_by_role("button", name="Show less Output")
        await expect(show_less).to_be_in_viewport(ratio=1)
        await show_less.click()
        await expect(output).to_have_attribute("data-expanded", "false")
        await expect(output.get_by_role("button", name=re.compile("^Show all"))).to_be_visible()
        await page.mouse.move(0, 0)
        await completed_rollout.capture(name=f"{completed_rollout.capture_name}_output_preview")
        shell_control = call_heading.get_by_role("button").first
        await expect(shell_control).to_be_in_viewport(ratio=1)
        await shell_control.click()
        await expect(shell_control).to_have_attribute("aria-expanded", "false")
        run_control = run_heading.get_by_role("button").first
        await expect(run_control).to_be_in_viewport(ratio=1)
        await run_control.click()
        await expect(run_control).to_have_attribute("aria-expanded", "false")


async def _open_discarded_recovery(page: Page) -> None:
    discarded = page.locator(".agentplane-disclosure-summary").filter(has_text="not retained in model context")
    await expect(discarded).to_have_count(1)
    await discarded.click()
    await expect(discarded).to_have_attribute("aria-expanded", "true")


@pytest_asyncio.fixture(loop_scope="session")
async def recovery_tools(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    await app.recovery("tools")
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    await _open_run(view.page)
    await _open_discarded_recovery(view.page)
    await _expand_tool_steps(view.page)
    tools = _tool_steps(view.page)
    # The discarded call uses DiscardedCard, not a normal StepLine.
    await expect(tools).to_have_count(3)
    await expect(view.page.get_by_text("Succeeded, then discarded from context", exact=True)).to_be_visible()
    await expect(tools.locator(".agentplane-disclosure-summary[aria-expanded='false']")).to_have_count(0)
    # Expansion resizes virtual rows and can still restore the reading anchor.
    # Position the screenshot only after the history's own layout work finishes.
    await expect(view.page.get_by_role("region", name="Thread history")).to_have_attribute(
        "data-layout-settled", "true"
    )
    # Fixture clicks must not select a hover state when the scene subsequently scrolls.
    await view.page.mouse.move(0, 0)
    return view


@DESKTOP_AND_MOBILE
async def test_recovery_messages_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("messages")
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_discarded_recovery(page)
    await expect(page.get_by_text("Remember the name in the margin", exact=True)).to_be_visible()
    await expect(page.locator('[aria-label="Not retained in context"]')).to_be_visible()
    await expect(page.locator('[aria-label="Retention unknown"]')).to_be_visible()
    await expect(page.locator(".agentplane-disclosure-summary[aria-expanded='true']").first).to_be_visible()
    await view.capture()


async def test_recovery_quiet_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("quiet")
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_tool_run(page)
    tools = _tool_steps(page)
    await expect(tools).to_have_count(2)
    await expect(tools.locator(".agentplane-disclosure-summary[aria-expanded='false']")).to_have_count(0)
    await expect(page.locator(".agentplane-step-details .agentplane-code-block").first).to_be_visible()
    await view.capture()


async def test_debug_history_latest_session_error_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture()


@MOBILE_ONLY
async def test_debug_history_latest_session_error_raw_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=False)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_debug_history_latest_session_interleaved_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.INTERLEAVED_EVENTS)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_debug_history_latest_session_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE_TOUCH], ids=["desktop", "mobile"])
async def test_command_progress_semantics(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.build(Scene.COMMAND_PROGRESS)
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    input_row = page.locator('[data-command-id="progress-input"]')
    await expect(input_row.locator('[data-stage="admitted"]')).to_be_visible()
    await expect(input_row.locator(".agentplane-command-light")).to_have_count(4)
    detail = input_row.get_by_role("tooltip")
    await expect(detail).to_be_hidden()
    model_row = page.locator('[data-command-id="progress-model"]')
    await expect(model_row).to_contain_text("test-model-next")
    await expect(model_row.locator(".agentplane-command-card-line .agentplane-command-progress")).to_be_visible()
    for command_id in ("progress-noop", "progress-noop-input"):
        no_op = page.locator(f'[data-command-id="{command_id}"]')
        await expect(no_op.get_by_text("No-op")).to_be_visible()
        await expect(no_op.locator(".agentplane-command-progress")).to_have_count(0)
    await input_row.scroll_into_view_if_needed()
    await view.capture()
    effected = page.locator('[data-command-id="progress-effected"]')
    await expect(effected).to_contain_text("applied-model")
    await expect(effected.locator(".agentplane-command-check")).to_have_count(1)
    history = page.get_by_role("region", name="Thread history")
    for row in (model_row, effected):
        assert await row.evaluate("node => !!node.closest('[aria-label=\"Thread history\"]')")
    assert await model_row.evaluate(
        "node => node.closest('[data-thread-anchor]').dataset.threadAnchor"
    ) < await effected.evaluate("node => node.closest('[data-thread-anchor]').dataset.threadAnchor")
    await model_row.scroll_into_view_if_needed()
    await view.capture(name=f"{view.capture_name}_controls", target=history)
    input_indicator = input_row.locator(".agentplane-command-progress-hit")
    if viewport.has_touch:
        await input_indicator.tap()
        await expect(input_row.locator('[data-touch-open="true"]')).to_be_visible()
    else:
        await input_indicator.hover()
    await expect(detail).to_be_visible()
    await expect(detail).to_be_in_viewport(ratio=1)
    await view.capture(name=f"{view.capture_name}_detail")
    effort_row = page.locator('[data-command-id="progress-effort"]')
    effort_indicator = effort_row.locator(".agentplane-command-progress-hit")
    if viewport.has_touch:
        await effort_indicator.tap()
        await expect(effort_row.locator('[data-touch-open="true"]')).to_be_visible()
    else:
        await effort_indicator.hover()
    await expect(effort_row.get_by_role("tooltip")).to_be_visible()
    await expect(effort_row.get_by_role("tooltip")).to_be_in_viewport(ratio=1)
    await view.capture(name=f"{view.capture_name}_effort_detail")
    applied_input = page.locator('[data-command-id="progress-applied-input"]')
    input_hit = applied_input.locator(".agentplane-command-progress-hit")
    await expect(applied_input.locator(".agentplane-command-light")).to_have_count(0)
    await expect(input_hit).to_have_css("opacity", "0")
    if viewport.has_touch:
        await applied_input.locator(".agentplane-user-bubble").tap()
    else:
        await applied_input.locator(".agentplane-user-bubble").hover()
    await expect(input_hit).to_have_css("opacity", "1")
    await view.capture(name=f"{view.capture_name}_applied_input", target=applied_input)


@DESKTOP_AND_MOBILE
async def test_dispatcher_queued_while_sandbox_wakes(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.enable_suspended_sandbox_wake_on_message()
    await app.remember_dispatch(SUSPENDED_THREAD)
    await app.mount_thread(SUSPENDED_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await expect(page.get_by_role("textbox", name="Message")).to_be_enabled()
    await expect(page.get_by_role("textbox", name="Message")).to_have_attribute(
        "placeholder",
        "Sandbox is suspended (on request). Wake on message is on; submitted commands will queue while it wakes. Enter sends, Shift+Enter or Ctrl+Enter for a new line",
    )
    await expect(page.get_by_text("Sandbox is suspended", exact=False)).to_have_count(0)
    await expect(page.get_by_role("button", name="Resume Sandbox")).to_have_count(0)
    row = page.locator('[data-command-id="queued-dispatch"]')
    await expect(row).to_contain_text("Sandbox is waking; message is queued")
    await expect(row.locator('[data-stage="queued"]')).to_be_visible()
    await expect(row.locator(".agentplane-command-light")).to_have_count(4)
    await expect(row.get_by_role("button", name="Retry")).to_have_count(0)
    await expect(row.get_by_role("button", name="Cancel")).to_have_count(0)
    await view.capture(name=f"{view.capture_name}_wake_composer_placeholder")
    await page.get_by_role("textbox", name="Message").fill("Please wake the Sandbox and continue.")
    await expect(page.get_by_role("button", name="Send")).to_be_enabled()
    await view.capture()


async def test_dispatcher_reports_why_a_sandbox_wake_failed(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.remember_failed_dispatch()
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    row = view.page.locator('[data-command-id="wake-deadline-dispatch"]')
    await expect(row.locator('[data-stage="dispatch_failed"]')).to_be_visible()
    await row.locator(".agentplane-command-progress-hit").hover()
    tooltip = row.get_by_role("tooltip")
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_contain_text("Sandbox did not become Ready within 10 minutes")
    await expect(tooltip).to_contain_text("Unschedulable: 0/2 nodes are available: PVC is not bound")
    await expect(tooltip).to_be_in_viewport(ratio=1)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE_TOUCH], ids=["desktop", "mobile"])
async def test_detail_of_a_short_message_stays_on_the_page(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.remember_short_refused_wake()
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    row = view.page.locator('[data-command-id="short-wake-refused"]')
    indicator = row.locator(".agentplane-command-progress-hit")
    if viewport.has_touch:
        await indicator.tap()
    else:
        await indicator.hover()
    tooltip = row.get_by_role("tooltip")
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_contain_text("cannot update resource")
    await expect(tooltip).to_be_in_viewport(ratio=1)
    assert await view.page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    ), "the open detail made the page scroll sideways"
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE_TOUCH], ids=["desktop", "mobile"])
async def test_cancel_unsent_input(view: VisualPage, app: AgentplaneFixture) -> None:
    await view.page.evaluate(
        "() => Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false })"
    )
    await app.build(Scene.REMEMBER_PENDING_INPUT)
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    row = view.page.locator('[data-command-id="locally-retained"]')
    await expect(row.get_by_role("button", name="Cancel")).to_be_visible()
    await expect(row.locator('[data-stage="local"]')).to_have_attribute(
        "aria-label", "Saved in browser · waiting for connection to send"
    )
    await view.capture(target=row)
    await row.get_by_role("button", name="Cancel").click()
    await expect(row).to_have_count(0)


async def test_debug_history_latest_session_pending_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.build(Scene.REMEMBER_PENDING_INPUT)
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await expect(page.locator('[data-thread-anchor="16"]')).to_be_visible()
    await expect(page.locator('.agentplane-user-bubble[data-message-phase="local"]')).to_be_visible()
    await view.capture()


async def test_debug_history_stderr_disclosure(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.INTERLEAVED_EVENTS)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    stderr = page.locator('[data-debug-observation="31"] .agentplane-disclosure-summary')
    await stderr.click()
    await expect(stderr).to_have_attribute("aria-expanded", "true")
    await view.capture()


async def test_thread_setup_progress(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.THREAD_SETUP_RUNNING)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    setup = page.locator(".agentplane-disclosure-summary").filter(has_text="Thread setup running")
    await expect(setup.get_by_text("Cloning into project...")).to_be_visible()
    await expect(setup.get_by_text("Updating files: 100%")).to_be_visible()
    await expect(setup.get_by_text("Updating files: 10%")).to_have_count(0)
    await expect(page.get_by_role("textbox", name="Message")).to_be_enabled()
    await expect(page.get_by_role("combobox", name="Model", exact=True)).to_be_enabled()
    await view.capture(name="thread_setup_live_progress")


async def test_thread_setup_output(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.THREAD_SETUP)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    setup = page.locator(".agentplane-disclosure-summary").filter(has_text="Thread setup complete")
    await setup.click()
    await expect(setup).to_have_attribute("aria-expanded", "true")
    await expect(page.get_by_text("Setup stdout")).to_be_visible()
    await expect(page.get_by_text("Setup stderr", exact=True).first).to_be_visible()
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_recovery_tool_lower_states(recovery_tools: VisualPage) -> None:
    view = recovery_tools
    page = view.page
    await _in_viewport(page.get_by_text("Failed, still in context", exact=True))
    await _focus(page, page.locator('[aria-label="Retention unknown"]').last)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_revised_recovery_tool_output(recovery_tools: VisualPage) -> None:
    view = recovery_tools
    page = view.page
    output = page.locator(".agentplane-output-label").filter(has_text="Continuation output")
    # The label is sticky: scroll its content into view from the content's natural position.
    await _focus(page, page.get_by_text("aborted", exact=True))
    await _in_viewport(output)
    await view.capture()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_reasoning_inside_tool_run(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standard_history(long_preview=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_run(page)
    reasoning = page.locator(".agentplane-step-details").filter(
        has=page.locator(".agentplane-step-title:text-is('Reasoning')")
    )
    title_size = await reasoning.locator(".agentplane-step-title").evaluate("el => getComputedStyle(el).fontSize")
    preview_size = await reasoning.locator(".agentplane-markdown--single-line").evaluate(
        "el => getComputedStyle(el).fontSize"
    )
    assert title_size == preview_size
    await reasoning.locator(".agentplane-disclosure-summary").first.click()
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_consecutive_reasoning_group_inside_tool_run(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.consecutive_reasoning_history()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_run(page)
    steps = page.locator(".agentplane-run-steps").first
    reasoning_group = steps.locator(":scope > .agentplane-collapsible-card").nth(1)
    summary = reasoning_group.locator(".agentplane-disclosure-summary").first
    await expect(summary).to_have_attribute("aria-expanded", "false")
    await expect(reasoning_group.locator(".agentplane-step-title")).to_have_text("Reasoning")
    await expect(reasoning_group.locator(".agentplane-step-preview .agentplane-markdown--single-line")).to_be_attached()
    await view.capture(target=page.locator("#app"))

    await summary.click()
    await expect(summary).to_have_attribute("aria-expanded", "true")
    await expect(reasoning_group.locator(".agentplane-disclosure-panel .agentplane-step-title")).to_have_text(
        ["Reasoning", "Reasoning", "Reasoning"]
    )
    await view.capture(name=f"{view.capture_name}_reasoning_items", target=page.locator("#app"))


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_reasoning_heading_sticks_at_history_bottom(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_body=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = await _open_standalone_reasoning(page)
    history = page.locator("[aria-label='Thread history']")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    control = reasoning.locator(".agentplane-disclosure-summary").first
    await expect(control).to_have_attribute("aria-expanded", "true")
    title_size = await reasoning.locator(".agentplane-step-title").evaluate("el => getComputedStyle(el).fontSize")
    preview_size = await reasoning.locator(".agentplane-markdown--single-line").evaluate(
        "el => getComputedStyle(el).fontSize"
    )
    assert title_size == preview_size
    await page.mouse.move(0, 0)
    await view.capture(target=view.page.locator("#app"))
    await expect(control).to_be_in_viewport(ratio=1)
    await control.click()
    await expect(control).to_have_attribute("aria-expanded", "false")
    await page.mouse.move(0, 0)
    await view.capture(name=f"{view.capture_name}_collapsed", target=page.locator("#app"))


async def test_standalone_reasoning_opens_session_standalone_reasoning_open(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.standalone_reasoning(long_preview=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = await _open_standalone_reasoning(page)
    await expect(reasoning.locator('a[href="https://example.test/projection"]')).to_have_text("projection path")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_standalone_reasoning_compact_markdown_and_code_fence(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(code_fence=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
    summary = reasoning.locator(".agentplane-disclosure-summary").first
    preview = reasoning.locator(".agentplane-step-preview .agentplane-markdown--single-line")
    await expect(summary).to_have_attribute("aria-expanded", "false")
    await expect(preview).to_contain_text("> Check")
    await expect(preview).to_contain_text("State | Count ; Ready | 2")
    await expect(preview).to_contain_text("• Parsed • Complete")
    await wait_for_stable(page)
    await view.capture(target=page.locator("#app"))

    await summary.click()
    await expect(summary).to_have_attribute("aria-expanded", "true")
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    await expect(reasoning.locator(".agentplane-code-block .cm-content")).to_be_attached()
    await wait_for_stable(page)
    await view.capture(name=f"{view.capture_name}_expanded", target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_shell_call_run_previews(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_run(page)
    await expect(page.locator(".agentplane-step-details .agentplane-step-preview").first).to_be_visible()
    await view.capture()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("anchor", ["4", "34"], ids=["user-message", "assistant-text"])
async def test_thread_evidence_panel(view: VisualPage, app: AgentplaneFixture, anchor: str) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    owner = page.locator(f'[data-thread-anchor="{anchor}"]')
    button = owner.locator("button.agentplane-evidence-toggle").first
    # The overlay button is reachable by keyboard even when another row covers its corner.
    await button.focus()
    await button.press("Enter")
    await expect(button).to_have_attribute("aria-expanded", "true")
    await expect(owner.locator("[data-evidence-observation]").first).to_be_visible()
    if anchor == "4":
        bubble = owner.locator(".agentplane-user-bubble")
        panel = owner.locator(".agentplane-disclosure").first
        await expect(bubble.locator(".agentplane-disclosure")).to_have_count(0)
        bubble_box = await bubble.bounding_box()
        panel_box = await panel.bounding_box()
        owner_box = await owner.bounding_box()
        assert bubble_box is not None
        assert panel_box is not None
        assert owner_box is not None
        assert panel_box["y"] >= bubble_box["y"] + bubble_box["height"]
        assert abs(panel_box["x"] - owner_box["x"]) <= 1
        assert abs(panel_box["width"] - owner_box["width"]) <= 1
    await button.blur()
    await page.mouse.move(0, 0)
    await view.capture()
    frames = owner.locator("[data-evidence-observation]").first
    await frames.get_by_role("button", name=re.compile("Observation .* raw frames")).click()
    await expect(frames.locator(".agentplane-code-block")).to_be_visible()
    await page.mouse.move(0, 0)
    await view.capture(name=f"{view.capture_name}_raw_frames")


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
async def test_bash_evidence_panel(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_run(page)
    owner = (
        page.locator(".agentplane-collapsible-card.agentplane-evidence-owner")
        .filter(has=page.locator(".agentplane-step-title:text-is('Bash')"))
        .first
    )
    await expect(owner.locator(".agentplane-step-title")).to_have_text("Bash")
    await owner.locator(".agentplane-step-details .agentplane-disclosure-summary").click()
    await expect(owner.locator(".agentplane-step-details .agentplane-code-block .cm-content").first).to_be_attached()
    button = owner.locator(".agentplane-step-inspect > .agentplane-evidence-toggle")
    await expect(button).to_be_attached()

    # Keyboard and pointer activation both reach the separate action beside the chevron.
    await button.focus()
    await button.press("Enter")
    await expect(button).to_have_attribute("aria-expanded", "true")
    await button.press("Enter")
    await button.blur()
    await page.mouse.move(0, 0)
    await owner.hover()
    await expect(button).to_have_css("opacity", "1")
    heading = owner.locator(".agentplane-step-details .agentplane-disclosure-heading").first
    button_box = await button.bounding_box()
    heading_box = await heading.bounding_box()
    assert button_box is not None
    assert heading_box is not None
    assert button_box["x"] >= heading_box["x"] - 1
    assert button_box["x"] + button_box["width"] <= heading_box["x"] + heading_box["width"] + 1

    await button.click()
    await expect(button).to_have_attribute("aria-expanded", "true")
    frames = owner.locator("[data-evidence-observation]").first
    await expect(frames).to_be_visible()
    await page.mouse.move(0, 0)
    await view.capture()
    await frames.get_by_role("button", name=re.compile("Observation .* raw frames")).click()
    await expect(frames.locator(".agentplane-code-block")).to_be_visible()
    await page.mouse.move(0, 0)
    await view.capture(name=f"{view.capture_name}_raw_frames")


@pytest.mark.parametrize("viewport", [MOBILE_TOUCH], ids=["mobile-touch"])
async def test_bash_evidence_button_reveals_on_tap(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_run(page)
    owner = (
        page.locator(".agentplane-collapsible-card.agentplane-evidence-owner")
        .filter(has=page.locator(".agentplane-step-title:text-is('Bash')"))
        .first
    )
    await expect(owner.locator(".agentplane-step-title")).to_have_text("Bash")
    await owner.locator(".agentplane-step-details .agentplane-disclosure-summary").tap()
    await expect(owner).to_have_attribute("data-evidence-revealed", "")
    button = owner.locator(".agentplane-step-inspect > .agentplane-evidence-toggle")
    await expect(button).to_have_css("opacity", "1")
    await button.tap()
    await expect(button).to_have_attribute("aria-expanded", "true")
    await expect(owner.locator("[data-evidence-observation]").first).to_be_visible()
    await view.capture()


@pytest.mark.parametrize(
    "target",
    ['[data-thread-anchor="4"] .agentplane-user-bubble', '[data-thread-anchor="34"] .agentplane-evidence-owner'],
    ids=['[data-thread-anchor="4"] .agentplane-user-bubble', '[data-thread-anchor="34"] .agentplane-evidence-owner'],
)
async def test_evidence_button_reveals_on_hover(view: VisualPage, app: AgentplaneFixture, target: str) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    owner = page.locator(target)
    await owner.hover()
    await expect(owner.get_by_role("button", name="Evidence")).to_have_css("opacity", "1")
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE_TOUCH], ids=["mobile-touch"])
async def test_evidence_button_reveals_on_tap(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    owner = page.locator('[data-thread-anchor="34"] .agentplane-evidence-owner')
    await owner.tap()
    await expect(owner).to_have_attribute("data-evidence-revealed", "")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_open_tool_calls_and_output_session_tool_payloads(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_tool_run(page)
    await expect(
        page.locator(".agentplane-output-disclosure .agentplane-disclosure-summary[aria-expanded='true']").first
    ).to_be_attached()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_open_tool_calls_and_output_session_shell_calls_open(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_tool_run(page)
    call = page.locator(".agentplane-step-details").filter(has_text="step 16")
    output_control = call.locator(".agentplane-output-disclosure .agentplane-disclosure-summary[aria-expanded='true']")
    output = call.locator(".agentplane-output-disclosure .agentplane-code-block")
    await _in_viewport(output_control)
    await expect(output).to_contain_text("step 16")
    await _in_viewport(output)
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize(
    ("call_text", "tool", "viewport"),
    [
        ("List every container and its status", "Bash", DESKTOP),
        ("List every container and its status", "Bash", MOBILE),
        ("Test Bank", "Shell", DESKTOP),
        ("Test Bank", "Shell", MOBILE),
    ],
    ids=[
        "list every container and its status-bash-desktop",
        "list every container and its status-bash-mobile",
        "test bank-shell-desktop",
        "test bank-shell-mobile",
    ],
)
async def test_shell_call_command_and_output(shell_calls: VisualPage, call_text: str, tool: str) -> None:
    view = shell_calls
    page = view.page
    await _open_tool_run(page)
    call = page.locator(".agentplane-step-details").filter(has_text=call_text)
    await expect(call.locator(".agentplane-step-title")).to_have_text(tool)
    await _focus(page, call.locator(".agentplane-clamped-block[data-label='Command']"))
    await _in_viewport(call.locator(".agentplane-output-label"))
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_shell_call_raw_arguments_and_json_output(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_run(page)
    call = (
        page.locator(".agentplane-step-details")
        .filter(has=page.locator(".agentplane-step-title:text-is('Shell')"))
        .first
    )
    await call.locator(".agentplane-disclosure-summary").first.click()
    await _focus(page, call.locator(".agentplane-step-controls"))
    await call.locator("label").filter(has_text=re.compile(r"^Raw$")).click()
    await expect(call.get_by_role("switch", name="Show raw arguments and output")).to_be_checked()
    arguments = call.locator(".agentplane-clamped-block[data-label='Arguments']")
    await expect(arguments).to_be_visible()
    await expect(call.locator(".agentplane-clamped-block[data-label='Command']")).to_have_count(0)
    await expect(call.locator(".agentplane-output-disclosure .agentplane-code-block")).to_contain_text(
        '"institution_name"'
    )
    await _focus(page, arguments)
    await expect(call.locator(".agentplane-step-controls")).to_be_in_viewport(ratio=1)
    await expect(call.locator(".agentplane-output-label")).to_be_in_viewport(ratio=1)
    await page.mouse.move(0, 0)
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_collapsed_steps_in_open_run_are_compact(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_run(page)
    await expect(page.locator(".agentplane-step-details [aria-busy='true']")).to_have_count(0)
    steps = page.locator(
        ".agentplane-run-steps .agentplane-step-details .agentplane-disclosure-summary[aria-expanded='false']"
    )
    await expect(steps.first).to_be_attached()
    # Both kinds share the step disclosure control. Its normal mobile min-height
    # and label padding must not turn every collapsed step into a full-size card.
    assert await steps.first.evaluate("el => getComputedStyle(el).minHeight") == "24px"
    assert (
        await steps.first.locator(".agentplane-disclosure-summary-content").evaluate(
            "el => getComputedStyle(el).paddingBlockStart"
        )
        == "0px"
    )
    await _focus(page, steps.first)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_expanded_command_uses_heading_to_collapse(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
    await _open_tool_run(page)
    command = (
        page.locator(".agentplane-clamped-block[data-label='Command']")
        .filter(has=page.get_by_role("button", name=re.compile("^Show all")))
        .first
    )
    await expect(command).to_be_attached()
    await command.get_by_role("button", name=re.compile("^Show all")).click()
    # The Show all control disappears after expansion, so use the expanded
    # block rather than a locator that keeps filtering for Show all.
    heading = page.locator(
        ".agentplane-clamped-block[data-label='Command'][data-expanded='true'] .agentplane-clamped-disclosure .agentplane-disclosure-heading"
    ).first
    await expect(heading.locator("button")).to_have_count(1)
    await expect(heading.get_by_role("button", name="Command", expanded=True)).to_be_visible()
    await _focus(page, heading)
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_expanded_shell_output_sticks_while_scrolling(shell_calls: VisualPage) -> None:
    view = shell_calls
    page = view.page
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
    await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
