"""Agentplane render-only checkpoints; interactions live in the feature tests."""

import pytest
import pytest_bazel

from agentplane.app.frontend.testing.visual_assertions import _open_phone_navigation
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

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend/testing:visual_fixtures
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


async def test_threads(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.page.wait_for_selector("a.agentplane-sidebar-group-name", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_threads_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
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
    await app.mount_app("/sandboxes")
    await view.page.wait_for_selector('[aria-label="Wake on message for ready-sandbox"]', state="attached")
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
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


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
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_status(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_policy(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=policy")
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
    await view.page.wait_for_selector(':text("Last observed Sandbox and Pod")', state="attached")
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


async def test_session_resume(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.build(Scene.ENDED_ATTACHMENT)
    await app.mount_thread(RUNNING_THREAD)
    await view.page.wait_for_selector('[aria-label="Harness not running"]', state="attached")
    await view.page.wait_for_selector('[aria-label="Resume harness"]', state="attached")
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
