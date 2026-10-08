"""Agentplane history visual behavior tests."""

import re
from textwrap import dedent

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import Page, expect

from agentplane.app.frontend.visual_app import IDLE_THREAD, RUNNING_THREAD, AgentplaneFixture
from agentplane.app.frontend.visual_assertions import (
    _expand_tool_steps,
    _focus,
    _in_viewport,
    _open_debug_history,
    _open_run,
    _open_tool_run,
    _rollout_run,
    _rollout_start,
)
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, MOBILE_TOUCH
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize(
    ("viewport", "color_scheme"),
    [(DESKTOP, "light"), (MOBILE, "light"), (DESKTOP, "dark")],
    ids=["desktop-light", "mobile-light", "desktop-dark"],
)
async def test_completed_rollout_overview(completed_rollout: VisualPage) -> None:
    await _rollout_start(completed_rollout)
    await completed_rollout.capture()


async def test_reported_rollout_overview(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.reported_rollout()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    await _rollout_start(view)
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
async def test_reported_rollout_run(view: VisualPage, app: AgentplaneFixture, position: str) -> None:
    await app.reported_rollout()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector("[aria-label='Thread history'][data-layout-settled='true']", state="attached")
    await view.check(context="fixture ready")
    await _rollout_run(view, position)
    await view.capture()


@pytest.mark.parametrize(
    ("expanded_output", "viewport"),
    [(False, DESKTOP), (False, MOBILE), (True, DESKTOP), (True, MOBILE)],
    ids=["false-desktop", "false-mobile", "true-desktop", "true-mobile"],
)
async def test_realistic_rollout_call(completed_rollout: VisualPage, expanded_output: bool) -> None:
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
    divider_edges = await heading.evaluate(
        dedent(
            "element => {\n                const heading = element.getBoundingClientRect();\n                const divider = getComputedStyle(element, '::after');\n                const card = element.closest('.agentplane-collapsible-card').getBoundingClientRect();\n                return [heading.left + parseFloat(divider.left) - card.left,\n                        card.right - heading.right + parseFloat(divider.right)];\n            }"
        )
    )
    assert all(abs(edge) <= 1 for edge in divider_edges), f"output divider escaped its card: {divider_edges}"
    if expanded_output:
        await _focus(page, output)
        await output.get_by_role("button", name=re.compile("^Show all")).click()
        await expect(output).to_have_attribute("data-expanded", "true")
        await output.evaluate(
            dedent(
                "element => {\n                  const history = element.closest('[aria-label=\"Thread history\"]');\n                  history.scrollTop += element.getBoundingClientRect().top - history.getBoundingClientRect().top + 300;\n                }"
            )
        )
        await wait_for_stable(page)
        await expect(call.locator(".agentplane-output-label")).to_be_in_viewport()
    else:
        await _focus(page, call.locator(".agentplane-clamped-block[data-label='Command']"))
    await page.mouse.move(0, 0)
    await completed_rollout.capture()


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
    tools = view.page.locator(".agentplane-step-details").filter(
        has_not=view.page.locator(".agentplane-step-title:text-is('Reasoning')")
    )
    # The discarded call uses DiscardedCard, not a normal StepLine.
    await expect(tools).to_have_count(3)
    await expect(view.page.get_by_text("Succeeded, then discarded from context", exact=True)).to_be_visible()
    await expect(tools.locator(".agentplane-disclosure-summary[aria-expanded='false']")).to_have_count(0)
    return view


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
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
    tools = page.locator(".agentplane-step-details").filter(
        has_not=page.locator(".agentplane-step-title:text-is('Reasoning')")
    )
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


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_debug_history_latest_session_error_raw_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=False)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_debug_history_latest_session_interleaved_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.interleaved_events()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_debug_history_latest_session_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await view.capture(target=view.page.locator("#app"))


async def test_debug_history_latest_session_pending_raw(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.pending_commands()
    await app.remember_pending_input()
    await app.mount_thread(RUNNING_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    await expect(page.locator('[data-thread-anchor="16"]')).to_be_visible()
    await expect(page.locator('.agentplane-user-bubble[data-message-phase="local"]')).to_be_visible()
    await view.capture()


async def test_debug_history_stderr_disclosure(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.interleaved_events()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_debug_history(page)
    stderr = page.locator('[data-debug-observation="31"] .agentplane-disclosure-summary')
    await stderr.click()
    await expect(stderr).to_have_attribute("aria-expanded", "true")
    await view.capture()


async def test_thread_setup_output(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.thread_setup()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    setup = page.locator(".agentplane-disclosure-summary").filter(has_text="Thread setup complete")
    await setup.click()
    await expect(setup).to_have_attribute("aria-expanded", "true")
    await expect(page.get_by_text("Setup stdout")).to_be_visible()
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_recovery_tool_lower_states(recovery_tools: VisualPage) -> None:
    view = recovery_tools
    page = view.page
    await _in_viewport(page.get_by_text("Failed, still in context", exact=True))
    await _focus(page, page.locator('[aria-label="Retention unknown"]').last)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_revised_recovery_tool_output(recovery_tools: VisualPage) -> None:
    view = recovery_tools
    page = view.page
    output = page.locator(".agentplane-output-label").filter(has_text="Continuation output")
    await _focus(page, output)
    await _in_viewport(page.get_by_text("aborted", exact=True))
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_reasoning_inside_tool_run(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standard_history(long_preview=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_run(page)
    reasoning = page.locator(".agentplane-step-details").filter(
        has=page.locator(".agentplane-step-title:text-is('Reasoning')")
    )
    await reasoning.locator(".agentplane-disclosure-summary").first.click()
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_reasoning_heading_sticks_at_history_bottom(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_body=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
    await reasoning.locator(".agentplane-disclosure-summary").first.click()
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    history = page.locator("[aria-label='Thread history']")
    await expect(history).to_have_attribute("data-layout-settled", "true")
    await history.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    await expect(reasoning.locator(".agentplane-disclosure-summary").first).to_have_attribute("aria-expanded", "true")
    await view.capture(target=view.page.locator("#app"))


async def test_standalone_reasoning_opens_session_standalone_reasoning_open(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.standalone_reasoning(long_preview=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
    summary = reasoning.locator(".agentplane-disclosure-summary")
    await summary.click()
    await expect(summary).to_have_attribute("aria-expanded", "true")
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    await expect(reasoning.locator('a[href="https://example.test/projection"]')).to_have_text("projection path")
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_standalone_reasoning_opens_session_reasoning_code_fence_open(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.standalone_reasoning(code_fence=True)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    reasoning = page.locator('[data-thread-anchor="20"] .agentplane-step-details')
    summary = reasoning.locator(".agentplane-disclosure-summary")
    await summary.click()
    await expect(summary).to_have_attribute("aria-expanded", "true")
    await expect(reasoning.locator(".agentplane-disclosure-panel .agentplane-markdown")).to_be_visible()
    await expect(reasoning.locator(".agentplane-code-block .cm-content")).to_be_attached()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_shell_call_run_previews(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_run(page)
    await expect(page.locator(".agentplane-step-details .agentplane-step-preview").first).to_be_visible()
    await view.capture()


@pytest.mark.parametrize(("anchor", "viewport"), [("4", DESKTOP), ("34", MOBILE)], ids=["4-desktop", "34-mobile"])
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
    await button.blur()
    await page.mouse.move(0, 0)
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_open_tool_calls_and_output_session_shell_calls_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_tool_run(page)
    await expect(
        page.locator(".agentplane-output-disclosure .agentplane-disclosure-summary[aria-expanded='true']").first
    ).to_be_attached()
    await expect(page.locator("[data-clamped='true']").first).to_be_attached()
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
async def test_shell_call_command_and_output(
    view: VisualPage, app: AgentplaneFixture, call_text: str, tool: str
) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_tool_run(page)
    call = page.locator(".agentplane-step-details").filter(has_text=call_text)
    await expect(call.locator(".agentplane-step-title")).to_have_text(tool)
    await _focus(page, call.locator(".agentplane-clamped-block[data-label='Command']"))
    await _in_viewport(call.locator(".agentplane-output-label"))
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_collapsed_steps_in_open_run_are_compact(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_expanded_command_uses_heading_to_collapse(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_expanded_shell_output_sticks_while_scrolling(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.shell_calls()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
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
