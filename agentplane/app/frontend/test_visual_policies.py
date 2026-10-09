"""Agentplane policies visual behavior tests."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture
from agentplane.app.frontend.visual_assertions import (
    _assert_phone_composer_layout,
    _focus,
    _in_viewport,
    _open_raw_switches,
    _open_select,
    _select_reconnect,
)
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_inline_action_review(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Review pending actions").click()
    await expect(page.get_by_role("button", name="Hide pending action details")).to_be_visible()
    await expect(page.locator(".action-affordance-notice .agentplane-code-block").first).to_be_visible()
    await view.capture()


async def test_inline_action_review_scrolls_to_decisions_actions_attention_composer_long_desktop(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.show_pending_actions()
    await app.long_pending_action()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Review pending actions").click()
    details = page.locator(".action-affordance-details:not([hidden])")
    await expect(details.get_by_role("button", name="Approve").first).to_be_attached()
    await details.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await details.evaluate("element => element.scrollTop") > 0
    await _in_viewport(details.get_by_role("button", name="Approve").last)
    await view.capture()


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_inline_action_review_scrolls_to_decisions_actions_attention_composer_long_phone(
    view: VisualPage, app: AgentplaneFixture
) -> None:
    await app.show_pending_actions()
    await app.long_pending_action()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Review pending actions").click()
    details = page.locator(".action-affordance-details:not([hidden])")
    await expect(details.get_by_role("button", name="Approve").first).to_be_attached()
    await details.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await details.evaluate("element => element.scrollTop") > 0
    await _in_viewport(details.get_by_role("button", name="Approve").last)
    await _assert_phone_composer_layout(page)
    await view.capture()


async def test_actions_raw_switches(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_app("/actions")
    await view.check(context="fixture ready")
    page = view.page
    await _open_raw_switches(page)
    raw_switches = page.locator("label").filter(has_text=re.compile("^Raw$"))
    await _focus(page, raw_switches.last)
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_sandbox_status_raw_switches(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    page = view.page
    await _open_raw_switches(page)
    await expect(page.locator(".agentplane-code-block").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_connections_settings_modal_connections(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    if page.viewport_size and page.viewport_size["width"] < 600:
        await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Settings").click()
    await expect(page.locator("[data-connection-id]").first).to_be_visible()
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        assert await page.locator(".agentplane-connections").evaluate("el => el.scrollWidth <= el.clientWidth")
        await expect(page.locator(".agentplane-connection-mobile-label").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_connection_sa_rebind_confirmation(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    if page.viewport_size and page.viewport_size["width"] < 600:
        await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Settings").click()
    picker = page.get_by_role("combobox", name="Service account for Claude desktop")
    await picker.click()
    await page.get_by_role("option", name="agentplane-visual/operator-assistant").click()
    await page.get_by_role("button", name="Apply").click()
    confirm = page.get_by_role("button", name="Confirm change")
    await expect(confirm).to_be_visible()
    await expect(page.get_by_text("Existing client tokens will act as", exact=False)).to_be_visible()
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        await confirm.scroll_into_view_if_needed()
    await view.capture()


async def test_oauth_clients_update_while_settings_stays_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    await page.get_by_role("button", name="Settings").click()
    await expect(page.get_by_text("Claude desktop")).to_be_visible()
    await app.publish_connection_rename()
    await expect(page.get_by_text("Updated OAuth client")).to_be_visible()
    await view.capture(name="oauth_clients_live_rename")


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_connections_settings_modal_connections_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Settings").click()
    await expect(page.locator("[data-connection-id]").first).to_be_visible()
    await page.mouse.move(0, 0)
    await view.capture(target=view.page.locator("#app"))


async def test_consent_reconnect_warning_desktop(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/connection-enrollments/test-only-opaque-handle")
    await view.check(context="fixture ready")
    page = view.page
    await _select_reconnect(page)
    await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_action_history_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.page.wait_for_selector('img[src^="data:image/"]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    receipt = page.get_by_text("list the test backup archive", exact=True)
    await _focus(page, receipt)
    await _in_viewport(page.get_by_text("History", exact=True))
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_action_history_diagram(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.page.wait_for_selector('img[src^="data:image/"]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    image = page.locator('img[src^="data:image/"]').first
    await _focus(page, image)
    await _in_viewport(page.get_by_text("render the test diagram", exact=True))
    await view.capture(target=view.page.locator("#app"))


async def test_action_history_raw_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.mount_app("/actions")
    await view.check(context="fixture ready")
    page = view.page
    result = page.get_by_text("Result", exact=True).first
    await _open_raw_switches(page)
    await _focus(page, result)
    await view.capture(target=view.page.locator("#app"))


async def test_action_history_paging_control(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.paginate_action_history()
    await app.show_pending_actions()
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.page.wait_for_selector('[data-testid="action-history-load-more"]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    load_more = page.get_by_test_id("action-history-load-more")
    await expect(load_more).to_have_text("Load more")
    await page.locator(".agentplane-shell-main").evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    # The scroll extent can grow after the first move. Bring the control into view after
    # that layout settles, then verify the final captured state.
    await load_more.scroll_into_view_if_needed()
    await wait_for_stable(page)
    await _in_viewport(load_more)
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_mcp_servers_linked_and_expired(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/mcp-servers")
    await view.page.wait_for_selector("[data-mcp-server]", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _in_viewport(page.locator('[data-mcp-server="linkage:example_docs"]').get_by_text("linked", exact=True))
    await _in_viewport(page.locator('[data-mcp-server="linkage:example_cluster"]').get_by_text("expired", exact=True))
    await view.capture(target=view.page.locator("#app"))


async def test_mcp_servers_linkage_and_health_update_while_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/mcp-servers")
    page = view.page
    docs = page.locator('[data-mcp-server="linkage:example_docs"]')
    await expect(docs.get_by_text("linked", exact=True)).to_be_visible()
    await app.publish_mcp_linkage_change()
    await expect(docs.get_by_text("expired", exact=True)).to_be_visible()
    await app.publish_mcp_health_change()
    await expect(docs.get_by_text("connect_failed", exact=True)).to_be_visible()
    await view.capture(name="mcp_linkage_and_health_live")


@pytest.mark.parametrize(
    ("focus_key", "focus_state", "other_key", "other_state", "viewport"),
    [
        ("linkage:example_pantry", "unlinked", "linkage:example_calendar", "degraded", MOBILE),
        ("group:example_notes", "available", "group:example_mail", "connect_failed", MOBILE),
    ],
    ids=[
        "linkage:example-pantry-unlinked-linkage:example-calendar-degraded-mobile",
        "group:example-notes-available-group:example-mail-connect-failed-mobile",
    ],
)
async def test_mcp_servers_lower_statuses_mcp_servers_phone(
    view: VisualPage, app: AgentplaneFixture, focus_key: str, focus_state: str, other_key: str, other_state: str
) -> None:
    await app.mount_app("/mcp-servers")
    await view.page.wait_for_selector("[data-mcp-server]", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    focused = page.locator(f'[data-mcp-server="{focus_key}"]').get_by_text(focus_state, exact=True)
    await _focus(page, focused)
    await _in_viewport(page.locator(f'[data-mcp-server="{other_key}"]').get_by_text(other_state, exact=True))
    await view.capture(target=view.page.locator("#app"))


async def test_mcp_servers_lower_statuses_mcp_servers(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/mcp-servers")
    await view.page.wait_for_selector("[data-mcp-server]", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    focused = page.locator('[data-mcp-server="group:example_notes"]').get_by_text("available", exact=True)
    await _focus(page, focused)
    await _in_viewport(page.locator('[data-mcp-server="group:example_mail"]').get_by_text("connect_failed", exact=True))
    # All lower states fit together at desktop width, so one focused image covers them.
    await _in_viewport(page.locator('[data-mcp-server="linkage:example_calendar"]').get_by_text("degraded", exact=True))
    await _in_viewport(page.locator('[data-mcp-server="linkage:example_pantry"]').get_by_text("unlinked", exact=True))
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_consent_reconnect_warning_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/connection-enrollments/test-only-opaque-handle")
    await view.check(context="fixture ready")
    page = view.page
    await _select_reconnect(page)
    await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))
    await _in_viewport(page.get_by_text(re.compile("I confirm replacing this Connection")))
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_consent_reconnect_decision_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/connection-enrollments/test-only-opaque-handle")
    await view.check(context="fixture ready")
    page = view.page
    await _select_reconnect(page)
    authorize = page.get_by_role("button", name="Authorize")
    await _focus(page, authorize)
    await _in_viewport(page.get_by_role("button", name="Deny"))
    await expect(authorize).to_be_disabled()
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_action_policy_selector_hides_picked_option(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes?preset=public-coder")
    await view.page.wait_for_selector(".mantine-Pill-root", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Action policy sets", available="harness-reviews", picked="public-coder")
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_egress_policy_selector_hides_picked_option(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes?preset=public-coder")
    await view.page.wait_for_selector('.mantine-Pill-root:has-text("github-public")', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Egress policies", available="pypi", picked="github-public", press_arrow_down=True)
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_grant_selector_hides_picked_option(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes?preset=public-coder")
    await view.page.wait_for_selector(".mantine-Pill-root", state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Kubernetes grants", available="config-read", picked="workspace-read")
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_sandbox_egress_pick_updates_options(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=egress&rules=ready-sandbox-github-public")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Grant egress policies", available="pypi")
    await page.get_by_role("option", name=re.compile("pypi")).click()
    await expect(page.locator(".mantine-Pill-root", has_text="pypi")).to_be_visible()
    await expect(page.get_by_role("option", name=re.compile("github-public"))).to_be_visible()
    await expect(page.get_by_role("option", name=re.compile("pypi"))).to_have_count(0)
    await page.mouse.move(0, 0)
    await expect(page.get_by_role("tooltip")).to_have_count(0)
    await wait_for_stable(page)
    await view.capture(target=view.page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
