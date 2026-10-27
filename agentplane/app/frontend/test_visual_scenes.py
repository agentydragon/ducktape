"""Agentplane render-only checkpoints; interactions live in the feature tests."""

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.visual_assertions import _focus, _open_phone_navigation
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE, MOBILE_ONLY
from agentplane.app.frontend.visual_app import (
    DELETED_SANDBOX_THREAD,
    IDLE_THREAD,
    RUNNING_THREAD,
    SUSPENDED_THREAD,
    UNNAMED_THREAD,
    AgentplaneFixture,
    Flag,
    Scene,
)
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@DESKTOP_AND_MOBILE
async def test_session_recovery_messages(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("messages")
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[aria-label="Retention unknown"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_recovery_tools(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("tools")
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[aria-label="Retention unknown"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_recovery_quiet(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.recovery("quiet")
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="50"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_error(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=False)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="6"]', state="attached")
    await view.page.wait_for_selector('.agentplane-thread-status-indicator[data-status="turn_error"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_error_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=True)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="8"]', state="attached")
    await view.page.wait_for_selector('.agentplane-thread-status-indicator[data-status="turn_error"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_interleaved(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.INTERLEAVED_EVENTS)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="18"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_lifecycle_group(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.LIFECYCLE_GROUP)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="50"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_thread_setup(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.THREAD_SETUP)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(':text("Thread setup complete")', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_thread_trace_menu(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    await view.capture()
    assert view.capture_name is not None
    await view.page.get_by_role("button", name="More", exact=True).click()
    await expect(view.page.get_by_role("menuitem", name="Find Session traces in Grafana")).to_be_visible()
    await view.capture(name=f"{view.capture_name}_thread_trace_menu")


async def test_threads(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.page.wait_for_selector(
        "a.agentplane-thread-list-group-link .agentplane-thread-list-group-name", state="attached"
    )
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_threads_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    """A phone opens on the landing page's Sandboxes and Threads, not behind the drawer."""
    await app.mount_app("/")
    home = view.page.get_by_role("region", name="Sandboxes and threads")
    await expect(home.locator(".agentplane-thread-list-row-detail time").first).to_be_visible()
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_threads_failed_turn(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.failed_turn(after_content=False)
    await app.mount_app("/")
    await view.page.wait_for_selector(".agentplane-thread-status-indicator[data-status='turn_error']", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_threads_provisioning(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ADD_PROVISIONING_SANDBOX)
    await app.mount_app("/")
    await view.page.wait_for_selector('a[href="#/sandboxes/test-provisioning"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_threads_updates_disconnected(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.THREAD_DATABASE_DISCONNECTED)
    await app.mount_app("/")
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_threads_watch_stale(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.WATCH_STALE)
    await app.mount_app("/")
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandboxes(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ADD_SUSPENDED_SANDBOXES)
    await app.mount_app("/sandboxes")
    await view.page.wait_for_selector('[aria-label="Wake on message for ready-sandbox"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize(
    ("sandbox", "pending", "reason"),
    [
        ("suspended-sandbox", False, "Resume it or enable wake on send"),
        ("pending-sandbox", False, "ImagePullBackOff"),
        ("ready-sandbox", True, "Check Open status"),
    ],
    ids=["suspended", "not-ready", "unconfirmed-open"],
)
async def test_start_session_disabled_tooltip(
    view: VisualPage, app: AgentplaneFixture, sandbox: str, pending: bool, reason: str
) -> None:
    page = view.page
    if pending:
        await page.evaluate(
            "name => sessionStorage.setItem(`agentplane:pending-open:${name}`, 'unconfirmed-session')", sandbox
        )
    await app.mount_app(f"/sandboxes/{sandbox}")
    await page.get_by_role("button", name="New session").click()
    await expect(page.get_by_role("combobox", name="Model")).not_to_have_value("")
    button = page.get_by_role("button", name="Start session", exact=True)
    await expect(button).to_be_disabled()
    wrapper = button.locator("..")
    await wrapper.hover()
    tooltip = page.get_by_role("tooltip")
    await expect(tooltip).to_contain_text(reason)
    await expect(tooltip).to_be_in_viewport(ratio=1)
    await view.capture(target=page.locator("#app"))
    await page.mouse.move(0, 0)
    await expect(tooltip).to_be_hidden()
    await wrapper.focus()
    await expect(tooltip).to_be_visible()
    await expect(tooltip).to_contain_text(reason)


@DESKTOP_AND_MOBILE
async def test_sandbox_start_session_suspended(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ADD_SUSPENDED_SANDBOXES)
    await app.mount_app("/sandboxes/idle-suspended-sandbox")
    await expect(view.page.get_by_text("Starting a session will wake the Sandbox.", exact=False)).to_be_visible()
    await view.page.get_by_role("button", name="New session").click()
    await expect(view.page.get_by_role("combobox", name="Model")).not_to_have_value("")
    await expect(view.page.get_by_role("button", name="Start session")).to_be_enabled()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_status_idle_suspended(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ADD_SUSPENDED_SANDBOXES)
    await app.mount_app("/sandboxes/idle-suspended-sandbox?tab=status")
    await expect(view.page.get_by_label("Idle suspend minutes for idle-suspended-sandbox")).to_have_value("30")
    await view.page.get_by_text("Runner diagnostics are unavailable until it is running.").wait_for(state="visible")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_sandboxes_stale(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.WATCH_STALE)
    await app.mount_app("/sandboxes")
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_actions_sidebar_under_threads(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_thread(IDLE_THREAD)
    await _open_phone_navigation(view.page)
    await view.page.wait_for_selector('.agentplane-actions-sidebar-toggle[aria-expanded="true"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_actions(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    page = view.page
    await page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_request_audit_details(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions/70000000-0000-4000-8000-000000000006")
    page = view.page
    # The summary line names the caller and when it asked.
    audit_toggle = page.get_by_role("button", name="ready-sandbox ·", exact=False)
    await expect(audit_toggle).to_have_count(1)
    await expect(audit_toggle.locator("time")).to_be_visible()
    await view.check(context="fixture ready")
    await audit_toggle.click()
    audit_item = page.locator(".agentplane-disclosure-item").filter(has=audit_toggle).first
    audit_body = audit_item.locator(".agentplane-disclosure-body")
    await expect(audit_body.get_by_text("Requested by agentplane-visual/ready-sandbox", exact=True)).to_be_visible()
    await expect(audit_body.get_by_text("70000000-0000-4000-8000-000000000006", exact=True)).to_be_visible()
    await _focus(page, audit_body)
    await page.mouse.move(0, 0)
    await view.capture(target=page.locator("#app"))


async def test_actions_history_groups_unavailable(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.ACTION_GROUPS_UNAVAILABLE)
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.page.wait_for_selector('[role="alert"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_actions_hidden_codepoints(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.HIDDEN_CODEPOINTS)
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".cm-agentplane-special-char-bidi", state="attached")
    await view.page.wait_for_selector(".cm-agentplane-special-char-ignorable", state="attached")
    await view.page.wait_for_selector(".cm-agentplane-special-char-control", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_consent(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/connection-enrollments/test-only-opaque-handle")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox")
    page = view.page
    await expect(page.get_by_role("table")).to_be_visible()
    await view.check(context="fixture ready")
    await view.capture(target=page.locator("#app"))
    assert view.capture_name is not None
    await page.get_by_role("button", name="More Sandbox actions").click()
    await expect(page.get_by_role("menuitem", name="Find Sandbox traces in Grafana")).to_be_visible()
    await expect(page.get_by_role("menuitem", name="Find Sandbox LLM traces in Langfuse")).to_be_visible()
    await view.capture(name=f"{view.capture_name}_sandbox_trace_menu")
    await page.get_by_role("button", name="More Sandbox actions").click()
    await page.get_by_role("button", name=f"More actions for {IDLE_THREAD}").click()
    await expect(page.get_by_role("menuitem", name="Find Session traces in Grafana")).to_be_visible()
    await view.capture(name=f"{view.capture_name}_session_trace_menu")

    await page.get_by_role("button", name="New session").click()
    await expect(page.get_by_role("combobox", name="Model")).not_to_have_value("")
    await expect(page.get_by_role("button", name="Start session")).to_be_enabled()
    await view.capture(target=page.locator("#app"), name=f"{view.capture_name}_new_session")
    for label, checkpoint in (
        ("Standing instructions", "standing_instructions"),
        ("Thread setup script", "thread_setup_script"),
    ):
        toggle = page.get_by_role("button", name=label, exact=True)
        await expect(toggle).to_be_visible()
        await toggle.click()
        textarea = page.get_by_role("textbox", name=label)
        await expect(textarea).to_be_visible()
        # Keep the editable body in the viewport so the checkpoint shows the actual terminal
        # content rather than only the sticky disclosure heading.
        await _focus(page, textarea)
        await page.mouse.move(0, 0)
        await view.capture(target=page.locator("#app"), name=f"{view.capture_name}_{checkpoint}_expanded")


@DESKTOP_AND_MOBILE
async def test_sandbox_status(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    page = view.page
    diagnostics = page.get_by_test_id("sandbox-runner-diagnostics")
    await view.page.get_by_text("dormant-session", exact=True).wait_for(state="visible")
    await view.page.get_by_text("ready-session", exact=True).wait_for(state="visible")
    await view.page.get_by_text("PID 18001", exact=True).wait_for(state="visible")
    sessions = page.get_by_role("heading", name="Sessions", exact=True)
    native_session = page.get_by_text("visual-native-session-01")
    dormant_session = page.get_by_text("dormant-session", exact=True)
    ready_session = page.get_by_text("ready-session", exact=True)
    await sessions.wait_for(state="visible")
    await native_session.wait_for(state="visible")
    await diagnostics.evaluate("element => element.scrollIntoView({ block: 'start', inline: 'nearest' })")
    await view.check(context="fixture ready")
    await expect(sessions).to_be_in_viewport()
    await expect(dormant_session).to_be_in_viewport()
    await expect(ready_session).to_be_in_viewport()
    await expect(native_session).to_be_in_viewport()
    await view.capture(target=diagnostics)


@DESKTOP_AND_MOBILE
async def test_sandbox_status_details(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    page = view.page
    await expect(page.get_by_role("rowheader", name="Wake on message")).to_be_visible()
    await expect(page.get_by_text("Loading runner diagnostics…")).to_have_count(0)
    await view.check(context="fixture ready")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_status_unmet_conditions(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/pending-sandbox?tab=status")
    page = view.page
    await expect(page.get_by_text("PodPending", exact=True)).to_be_visible()
    await expect(page.get_by_text("ImagePullBackOff")).to_be_visible()
    await expect(page.get_by_text("Loading runner diagnostics…")).to_have_count(0)
    await view.check(context="fixture ready")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_policy(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=policy")
    await expect(view.page.get_by_role("img", name="Refused")).to_have_count(1)
    await expect(view.page.get_by_role("img", name="Not yet judged")).to_have_count(1)
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_policy_rules(view: VisualPage, app: AgentplaneFixture) -> None:
    open_rules = "ready-sandbox-k2m9x,ready-sandbox-github-public,ready-sandbox-push-afternoon"
    await app.mount_app(f"/sandboxes/ready-sandbox?tab=policy&rules={open_rules}")
    await expect(view.page.get_by_role("button", name="Hide rules")).to_have_count(3)
    await expect(view.page.get_by_text("harness-release: no such PolicySet")).to_be_visible()
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_policy_refused_binding_hover(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=policy")
    await view.page.get_by_role("img", name="Refused").hover()
    await expect(view.page.get_by_role("tooltip")).to_contain_text("harness-release")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_deleted_sandbox(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(DELETED_SANDBOX_THREAD)
    await view.page.wait_for_selector('[role="status"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_suspended_sandbox(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(SUSPENDED_THREAD)
    await view.page.wait_for_selector('[aria-label="Sandbox suspended"]', state="attached")
    field = view.page.get_by_role("textbox", name="Message")
    await expect(field).to_be_disabled()
    await expect(field).to_have_attribute(
        "placeholder",
        "Sandbox is suspended. Wake on message is off; submitted commands stay queued until you resume it manually.",
    )
    await expect(view.page.get_by_text("Sandbox is suspended.", exact=True)).to_have_count(0)
    await expect(view.page.get_by_role("button", name="Interrupt")).to_have_count(0)
    await expect(view.page.get_by_role("button", name="Send")).to_have_count(0)
    await expect(view.page.get_by_role("button", name="Resume Sandbox")).to_be_visible()
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_inventory_stale(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.WATCH_STALE)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_inventory_stale_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.WATCH_STALE)
    await app.mount_thread(DELETED_SANDBOX_THREAD)
    await view.page.wait_for_selector(':text("Current availability unknown")', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_inventory_dropped(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.INVENTORY_STREAM_DROPPED)
    await app.age_outage(90000)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_inventory_dropped_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.INVENTORY_STREAM_DROPPED)
    await app.age_outage(90000)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_unnamed(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(UNNAMED_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_markdown_code_fence(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.MARKDOWN_CODE_FENCE)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-code-block", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_session_standalone_reasoning(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning()
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(
        '[data-thread-anchor="20"] .agentplane-step-preview .agentplane-markdown--single-line', state="attached"
    )
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_session_standalone_reasoning_preview(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(long_preview=True)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(
        '[data-thread-anchor="20"] .agentplane-step-details .agentplane-disclosure-summary:not(:has(a))',
        state="attached",
    )
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_session_unfinished_reasoning(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.UNFINISHED_REASONING)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(".agentplane-step-title--streaming", state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_reasoning_code_fence(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.standalone_reasoning(code_fence=True)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector(
        '[data-thread-anchor="20"] .agentplane-step-preview .agentplane-code-inline', state="attached"
    )
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_session_states(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="failed"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="noop"]', state="attached")
    await view.page.wait_for_selector('.agentplane-thread-status-indicator[data-status="running"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_streaming_interleaved(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.STREAMING_INTERLEAVED)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('.agentplane-streaming-cursor[aria-label="Streaming"]', state="attached")
    await view.page.wait_for_selector('[aria-label="Thread history"][data-layout-settled="true"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator(".agentplane-shell-main-content"))


@DESKTOP_AND_MOBILE
async def test_session_dormant(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.DORMANT_SESSION)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[aria-label="Harness not running"]', state="attached")
    await expect(view.page.get_by_role("textbox", name="Message")).to_be_enabled()
    await expect(view.page.get_by_role("combobox", name="Model", exact=True)).to_be_enabled()
    await expect(view.page.get_by_role("combobox", name="Reasoning effort")).to_be_enabled()
    await expect(view.page.get_by_role("button", name="Resume harness")).to_have_count(0)
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_pending(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.build(Scene.REMEMBER_PENDING_INPUT)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-command-id="queued-model"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="local"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_pending_input_echo(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.PENDING_INPUT_ECHO)
    await app.build(Scene.REMEMBER_INPUT_ECHO)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector(
        '[data-command-id="locally-retained"] .agentplane-verbatim:has-text("This browser already knows the text it sent.")',
        state="attached",
    )
    await view.page.wait_for_selector(
        '[data-command-id="remote-pending"] :text("Loading complete revision…")', state="attached"
    )
    await view.check(context="fixture ready")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_session_pending_failed(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.COMMAND_ADMISSION_TIMED_OUT)
    await app.build(Scene.PENDING_COMMANDS)
    await app.build(Scene.REMEMBER_PENDING_INPUT)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-stage="unconfirmed"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="local"]', state="attached")
    await view.page.locator(".agentplane-user-message-aside").get_by_role("button", name="Retry").wait_for()
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_pending_controls(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.PENDING_COMMANDS)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-command-id="queued-interrupt"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_command_outcomes_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.COMMAND_OUTCOMES)
    await app.build(Scene.REMEMBER_SETTLED_COMMANDS)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-command-id="queued-model"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="failed"]', state="attached")
    await view.page.wait_for_selector('.agentplane-user-bubble[data-message-phase="noop"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


async def test_session_catching_up(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.ENTITY_SEGMENTS_WITHHELD)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[data-thread-catchup="true"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_session_sync_unavailable(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.SYNC_SCOPE_UNAVAILABLE)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[role="alert"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


async def test_session_sync_reconnecting(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.ENTITY_STREAM_DISCONNECTED)
    await app.age_outage(10000)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[aria-label="Runner feed active · harness running"]', state="attached")
    await view.page.wait_for_selector('[data-connection="degraded"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_sync_reconnecting_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.ENTITY_STREAM_DISCONNECTED)
    await app.age_outage(90000)
    await app.mount_thread(IDLE_THREAD)
    await view.page.wait_for_selector('[aria-label="Runner feed active · harness running"]', state="attached")
    await view.page.wait_for_selector('[data-connection="stale"]', state="attached")
    await view.page.wait_for_selector('[data-thread-anchor="34"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


@MOBILE_ONLY
async def test_session_states_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[data-thread-anchor="16"]', state="attached")
    await view.check(context="fixture ready")
    await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
