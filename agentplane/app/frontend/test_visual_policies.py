"""Agentplane policies visual behavior tests."""

import re

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.visual_assertions import (
    _focus,
    _in_viewport,
    _open_phone_navigation,
    _open_raw_switches,
    _open_select,
    _open_settings,
    _select_reconnect,
)
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE, MOBILE_ONLY
from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture, Flag, Scene
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@DESKTOP_AND_MOBILE
async def test_actions_sidebar_queue(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    section = page.locator(".agentplane-actions-sidebar")
    await expect(section.locator(".agentplane-actions-sidebar-toggle")).to_have_attribute("aria-expanded", "true")
    await expect(section.get_by_text("restart the test backup service")).to_be_visible()
    await expect(page.get_by_label("Message")).to_have_count(1)
    await view.capture()


@MOBILE_ONLY
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
async def test_actions_attention_strip_over_thread(view: VisualPage, app: AgentplaneFixture, expanded: bool) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    strip = page.locator(".agentplane-actions-attention")
    toggle = strip.get_by_role("button", name="waiting for review")
    await expect(toggle).to_have_attribute("aria-expanded", "false")
    if expanded:
        await toggle.click()
        await expect(strip.get_by_text("restart the test backup service")).to_be_visible()
    await expect(page.get_by_label("Message")).to_be_visible()
    await view.check(context="Actions strip over the open thread")
    await view.capture()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("sidebar_open", [False, True], ids=["sidebar_closed", "sidebar_open"])
@pytest.mark.parametrize(
    "route",
    ["/", f"/threads/{IDLE_THREAD}", "/sandboxes", "/sandboxes/ready-sandbox", "/actions"],
    ids=["landing", "thread", "sandboxes", "sandbox", "actions"],
)
async def test_pending_actions_on_every_route(
    view: VisualPage, app: AgentplaneFixture, route: str, sidebar_open: bool
) -> None:
    """Where pending Actions show on each top-level route: the sidebar queue while it is open, the
    strip under the top bar while it is closed, and neither on the Actions page, which is the queue."""
    await app.set_flag(Flag.PENDING_ACTIONS)
    if route.startswith("/threads/"):
        await app.mount_thread(IDLE_THREAD)
    else:
        await app.mount_app(route)
    await view.check(context="fixture ready")
    page = view.page
    sidebar = page.get_by_role("navigation", name="Threads")
    if await sidebar.is_visible() != sidebar_open:
        await page.get_by_role("button", name="Toggle navigation").click()
    await expect(sidebar).to_be_visible(visible=sidebar_open)
    strip = page.locator(".agentplane-actions-attention")
    await expect(strip).to_have_count(0 if sidebar_open or route == "/actions" else 1)
    await view.capture(target=page.locator("#app"))


async def test_actions_sidebar_resize(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    section = page.locator(".agentplane-actions-sidebar")
    await expect(section.locator(".agentplane-actions-sidebar-toggle")).to_have_attribute("aria-expanded", "true")
    handle = section.get_by_role("separator", name="Resize Actions area")
    box = await handle.bounding_box()
    if box is None:
        raise AssertionError("Actions resize handle has no layout box")

    initial_height = await section.evaluate("element => element.getBoundingClientRect().height")
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(start_x, start_y - 96, steps=6)
    await page.mouse.up()

    resized_height = await section.evaluate("element => element.getBoundingClientRect().height")
    assert resized_height >= initial_height + 90
    await view.check(context="Actions area resized")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
async def test_sidebar_compact_pod_approval(view: VisualPage, app: AgentplaneFixture, expanded: bool) -> None:
    await app.set_flag(Flag.COMPACT_POD_ACTION)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    await _open_phone_navigation(page)
    section = page.locator(".agentplane-actions-sidebar")
    await expect(section.get_by_text("inspect running demo pods")).to_be_visible()
    await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text(
        "List pods in namespace test-apps"
    )
    await expect(section.get_by_role("link", name="View details for inspect running demo pods")).to_be_visible()
    await expect(section.get_by_text("kubernetes_admin / pods_list_in_namespace", exact=True)).to_have_count(0)
    if expanded:
        disclosure = section.get_by_role("button", name="Expand inspect running demo pods")
        bounds = await disclosure.bounding_box()
        assert bounds is not None
        await disclosure.click(position={"x": bounds["width"] - 4, "y": bounds["height"] - 3})
        preview = section.locator(".agentplane-actions-sidebar-preview")
        await expect(preview.get_by_text("label selector app=demo")).to_be_visible()
        await expect(preview.get_by_text("field selector status.phase=Running")).to_be_visible()
        await expect(preview.get_by_text("List pods in namespace", exact=False)).to_have_count(0)
        await expect(section.get_by_text("test-apps", exact=True)).to_have_count(1)
        await expect(section.get_by_text("Requested by", exact=False)).to_have_count(0)
        await expect(section.get_by_text("Check the exact call before approving.", exact=True)).to_have_count(0)
        await expect(section.get_by_text("app=demo")).to_be_visible()
        await expect(section.get_by_role("button", name="Deny inspect running demo pods")).to_be_visible()
        await expect(section.get_by_role("button", name="Approve inspect running demo pods")).to_be_visible()
    else:
        await expect(section.get_by_text("Filters")).to_be_visible()
        await expect(
            section.locator(".agentplane-actions-sidebar-collapsed-preview").get_by_text("test-apps")
        ).to_have_count(0)
        await expect(section.get_by_text("test-apps", exact=True)).to_have_count(1)
        await expect(section.get_by_role("button", name="Approve inspect running demo pods")).to_be_visible()
    await view.capture()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize(
    ("group", "name", "arguments", "visible"),
    [
        (
            "kubernetes_admin",
            "resources_get",
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
            "API version: apps/v1",
        ),
        (
            "kubernetes_admin",
            "pods_list_in_namespace",
            {"namespace": "apps", "labelSelector": "app=web", "fieldSelector": "status.phase=Running"},
            "status.phase=Running",
        ),
        (
            "kubernetes_admin",
            "resources_list",
            {"apiVersion": "v1", "kind": "Pod", "namespace": "apps", "labelSelector": "app=web"},
            "app=web",
        ),
        ("kubernetes_admin", "pods_log", {"name": "web-0", "previous": True, "tail": -1}, "previous: yes"),
        (
            "kubernetes_admin",
            "resources_delete",
            {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps", "gracePeriodSeconds": 0},
            "grace period: 0s",
        ),
        ("kubernetes_admin", "events_list", {"namespace": "apps", "fieldSelector": "type=Warning"}, "type=Warning"),
        (
            "github",
            "create_pull_request",
            {
                "owner": "example",
                "repo": "repo",
                "title": "Update docs",
                "head": "docs",
                "base": "devel",
                "draft": True,
            },
            "Update docs",
        ),
        (
            "kubernetes_admin",
            "pods_exec",
            {"name": "web-0", "namespace": "apps", "container": "main", "command": ["printenv", "POD_NAME"]},
            "printenv POD_NAME",
        ),
        (
            "gmail",
            "drafts_create",
            {"to": ["reviewer@example.com"], "subject": "Deployment review", "body": "Please review."},
            "reviewer@example.com",
        ),
        (
            "gmail",
            "threads_list",
            {"q": "from:deployments@example.com", "maxResults": 5},
            "from:deployments@example.com",
        ),
        ("grocy_sf", "products_list", {"detail": "full"}, "Full Product records"),
        ("grocy_sf", "quantity_units_list", {}, "Quantity unit names"),
        ("grocy_sf", "get_system_info", {}, "No arguments."),
        (
            "tana",
            "get_or_create_calendar_node",
            {"workspaceId": "workspace-123", "granularity": "week", "date": "2026-10-12"},
            "Granularity: week",
        ),
    ],
    ids=[
        "resource-get",
        "pods-in-namespace",
        "resource-list",
        "pod-log",
        "resource-delete",
        "events-list",
        "github-pr",
        "pod-exec",
        "gmail-draft",
        "gmail-search",
        "grocy-products",
        "grocy-units",
        "grocy-system-info",
        "tana-calendar-node",
    ],
)
async def test_compact_action_chips(
    view: VisualPage, app: AgentplaneFixture, group: str, name: str, arguments: dict[str, object], visible: str
) -> None:
    await app.show_action_preview(group, name, arguments)
    await app.mount_thread(IDLE_THREAD)
    await _open_phone_navigation(view.page)
    section = view.page.locator(".agentplane-actions-sidebar")
    if group == "kubernetes_admin" and name == "pods_list_in_namespace":
        title = "inspect running demo pods"
    else:
        title = f"review {name.replace('_', ' ')}"
    disclosure = section.get_by_role("button", name=f"Expand {title}")
    bounds = await disclosure.bounding_box()
    assert bounds is not None
    await disclosure.click(position={"x": bounds["width"] - 4, "y": bounds["height"] - 3})
    await expect(section.get_by_text(visible)).to_be_visible()
    if group == "kubernetes_admin" and name == "pods_list_in_namespace":
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text("List pods in namespace")
    if group == "gmail" and name == "threads_list":
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text("Gmail: Search threads")
        await expect(
            section.locator(".agentplane-actions-sidebar-preview").get_by_text("Maximum results: 5", exact=True)
        ).to_be_visible()
    if group == "grocy_sf":
        expected_label = {
            "products_list": "List Grocy products",
            "quantity_units_list": "List quantity units",
            "get_system_info": "Show Grocy system information",
        }[name]
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text(expected_label)
    if group == "tana" and name == "get_or_create_calendar_node":
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text(
            "Get or create Tana calendar node"
        )
    await expect(section.get_by_role("link", name=f"View details for {title}")).to_be_visible()
    if group == "kubernetes_admin" and name in {"resources_get", "pods_list_in_namespace", "pods_log"}:
        await expect(section.get_by_role("button", name=f"Approve {title}")).to_be_visible()
    if group == "kubernetes_admin" and name == "pods_log":
        await expect(section.get_by_text("container: (default)", exact=True)).to_be_visible()
    await view.capture(target=section)


@pytest.mark.parametrize(
    ("group", "name"),
    [("ssh", "list_targets"), ("uncatalogued", "unknown_action")],
    ids=["unregistered-action", "unknown-action"],
)
async def test_action_argument_fallbacks_use_json_view(
    view: VisualPage, app: AgentplaneFixture, group: str, name: str
) -> None:
    await app.show_action_preview(group, name, {"vendor_field": "fixture"})
    await app.mount_thread(IDLE_THREAD)
    page = view.page

    title = f"review {name.replace('_', ' ')}"
    section = page.locator(".agentplane-actions-sidebar")
    await section.get_by_role("button", name=f"Expand {title}").click()
    preview = section.locator(".agentplane-actions-sidebar-preview")
    await expect(preview).to_contain_text('"vendor_field": "fixture"')
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
async def test_compact_pr_with_description_requires_review(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_action_preview(
        "github",
        "create_pull_request",
        {
            "owner": "example",
            "repo": "repo",
            "title": "Update docs",
            "head": "docs",
            "base": "devel",
            "body": "Important PR description that must be read before approval.",
        },
    )
    await app.mount_thread(IDLE_THREAD)
    section = view.page.locator(".agentplane-actions-sidebar")
    await _open_phone_navigation(view.page)
    await section.get_by_role("button", name="Expand review create pull request").click()
    await expect(section.get_by_text("description: open Review")).to_be_visible()
    await expect(section.get_by_role("button", name="Approve review create pull request")).to_have_count(0)
    await expect(section.get_by_role("link", name="View details for review create pull request")).to_be_visible()
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
async def test_wide_action_decisions(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    page = view.page
    approve = page.get_by_role("button", name="Approve").first
    deny = page.get_by_role("button", name="Deny").first
    await expect(approve).to_be_visible()
    await expect(deny).to_be_visible()
    if viewport == DESKTOP:
        assert await approve.evaluate("element => element.getBoundingClientRect().width") >= 112
        assert await deny.evaluate("element => element.getBoundingClientRect().width") >= 112
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_detail_ssh_raw_view(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions/70000000-0000-4000-8000-000000000006")
    await view.check(context="fixture ready")
    page = view.page
    await expect(
        page.locator(".agentplane-shell-main-content").get_by_text("restart the test backup service")
    ).to_be_visible()
    await expect(page.get_by_role("button", name="Approve")).to_be_visible()
    await expect(page.get_by_role("button", name="Deny")).to_be_visible()
    await view.capture(target=page.locator("#app"), name=f"action_detail_ssh_pretty_{viewport.width}")
    await _open_raw_switches(page)
    await expect(page.get_by_role("switch", name="Raw")).to_be_checked()
    await expect(page.get_by_text('"command": "systemctl --user restart test-backup.service')).to_be_visible()
    await view.capture(target=page.locator("#app"), name=f"action_detail_ssh_raw_{viewport.width}")


@DESKTOP_AND_MOBILE
async def test_action_detail_external_grant_audit(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions/70000000-0000-4000-8000-000000000001")
    await view.check(context="fixture ready")
    page = view.page
    audit = page.get_by_text("Request & grant audit details")
    await expect(audit).to_be_visible()
    await audit.click()
    await expect(page.get_by_text("test-external-client")).to_be_visible()
    await view.capture(target=page.locator("#app"))


async def capture_long_ssh_preview_scrolled(view: VisualPage, app: AgentplaneFixture) -> None:
    """The fully expanded 55-line SSH preview stays a bounded box that scrolls to its end."""
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.build(Scene.LONG_PENDING_ACTION)
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await _open_phone_navigation(page)
    section = page.locator(".agentplane-actions-sidebar")
    await section.get_by_role("button", name="Expand restart the test backup service").click()
    preview = section.locator(".agentplane-actions-sidebar-preview").last
    await expect(preview.get_by_role("button", name="Show all 55 lines")).to_be_visible()
    await preview.get_by_role("button", name="Show all 55 lines").click()
    await preview.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await preview.evaluate("element => element.scrollTop") > 0
    await view.capture(target=section)


async def test_sidebar_long_ssh_preview_is_bounded_desktop(view: VisualPage, app: AgentplaneFixture) -> None:
    await capture_long_ssh_preview_scrolled(view, app)


@MOBILE_ONLY
async def test_sidebar_long_ssh_preview_is_bounded_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await capture_long_ssh_preview_scrolled(view, app)


async def test_actions_raw_switches(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await view.check(context="fixture ready")
    page = view.page
    await _open_raw_switches(page)
    raw_switches = page.locator("label").filter(has_text=re.compile("^Raw$"))
    await _focus(page, raw_switches.last)
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_status_raw_switches(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=status")
    await view.check(context="fixture ready")
    page = view.page
    await _open_raw_switches(page)
    await expect(page.locator(".agentplane-code-block").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_external_caller_grants(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    await _open_settings(page)
    await page.get_by_role("button", name="View grants", exact=True).first.click()
    grants = page.get_by_role("region", name="Grants for agentplane-test/personal")
    await expect(grants.get_by_role("heading", name="Egress", exact=True)).to_be_visible()
    await expect(grants.get_by_role("heading", name="Action policy", exact=True)).to_be_visible()
    await expect(grants.get_by_role("button", name="Refresh grants")).to_be_visible()
    await expect(grants.get_by_role("button", name="Revoke", exact=True)).to_have_count(0)
    await expect(grants.get_by_role("button", name="Rules", exact=True).first).to_be_visible()
    await grants.get_by_role("button", name="Rules", exact=True).first.click()
    assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
    await view.capture(target=page.locator(".mantine-Modal-content"))


@DESKTOP_AND_MOBILE
async def test_connections_settings_modal_connections(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_settings(page)
    await expect(page.locator("[data-connection-id]").first).to_be_visible()
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        assert await page.locator(".agentplane-connections").evaluate("el => el.scrollWidth <= el.clientWidth")
        await expect(page.locator(".agentplane-connection-mobile-label").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_connection_sa_rebind_confirmation(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_settings(page)
    picker = page.get_by_role("combobox", name="Service account for Claude desktop")
    await picker.click()
    await page.get_by_role("option", name="agentplane-visual/operator-assistant").click()
    await page.get_by_role("button", name="Apply").click()
    confirm = page.get_by_role("button", name="Confirm change")
    await expect(confirm).to_be_visible()
    await expect(page.get_by_text("Existing client tokens will act as", exact=False)).to_be_visible()
    row = page.locator("[data-connection-id]").filter(has_text="Claude desktop")
    await expect(row.get_by_role("button", name="Confirm change")).to_be_visible()
    await expect(row.get_by_role("button", name="Apply")).to_have_count(0)
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        await confirm.scroll_into_view_if_needed()
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_connection_unlink_confirmation(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_settings(page)
    row = page.locator("[data-connection-id]").filter(has_text="Claude desktop")
    await row.get_by_role("button", name="Unlink", exact=True).click()
    confirm = row.get_by_role("button", name="Confirm unlink")
    await expect(confirm).to_be_visible()
    await expect(row.get_by_text("Already claimed executions are not stopped", exact=False)).to_be_visible()
    await expect(row.get_by_role("button", name="Unlink", exact=True)).to_have_count(0)
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        await confirm.scroll_into_view_if_needed()
    await view.capture()


async def test_oauth_clients_update_while_settings_stays_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    await _open_settings(page)
    await expect(page.get_by_text("Claude desktop")).to_be_visible()
    await app.publish(Flag.CONNECTION_RENAMED, "/connections/stream")
    await expect(page.get_by_text("Updated OAuth client")).to_be_visible()
    await view.capture(name="oauth_clients_live_rename")


@MOBILE_ONLY
async def test_connections_settings_modal_connections_phone(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_settings(page)
    await expect(page.locator("[data-connection-id]").first).to_be_visible()
    await page.mouse.move(0, 0)
    await view.capture(target=view.page.locator("#app"))


@pytest_asyncio.fixture(loop_scope="session")
async def reconnect_review(view: VisualPage, app: AgentplaneFixture) -> VisualPage:
    """The consent page with an existing Connection picked for reconnecting."""
    await app.mount_app("/connection-enrollments/test-only-opaque-handle")
    await view.check(context="fixture ready")
    await _select_reconnect(view.page)
    return view


async def test_consent_reconnect_warning_desktop(reconnect_review: VisualPage) -> None:
    view = reconnect_review
    page = view.page
    await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_history_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await view.page.wait_for_selector(".agentplane-disclosure-summary", state="attached")
    await view.page.wait_for_selector('img[src^="data:image/"]', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    receipt = page.get_by_text("list the test backup archive", exact=True)
    await _focus(page, receipt)
    await _in_viewport(page.get_by_text("History", exact=True))
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_gmail_draft_history_keeps_request_and_result_together(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.GMAIL_DRAFT_HISTORY)
    await app.mount_app("/actions")
    page = view.page
    await expect(page.get_by_text("prepare the release email", exact=True)).to_be_visible()
    await expect(page.get_by_role("link", name="Release notes", exact=True)).to_be_visible()
    await expect(page.get_by_text("Please review it before sending.", exact=True)).to_be_visible()
    await expect(page.get_by_role("link", name="Reply in Gmail thread")).to_be_visible()
    await expect(page.get_by_text("Result", exact=True)).to_have_count(0)
    await view.check(context="Gmail draft request and result rendered")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_history_diagram(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
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
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await view.check(context="fixture ready")
    page = view.page
    result = page.get_by_text("Result", exact=True).first
    await _open_raw_switches(page)
    await _focus(page, result)
    await view.capture(target=view.page.locator("#app"))


async def test_action_history_paging_control(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PAGED_ACTION_HISTORY)
    await app.set_flag(Flag.PENDING_ACTIONS)
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


@DESKTOP_AND_MOBILE
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
    await app.publish(Flag.MCP_LINKAGE_CHANGED, "/mcp-servers/stream")
    await expect(docs.get_by_text("expired", exact=True)).to_be_visible()
    await app.publish(Flag.MCP_HEALTH_CHANGED, "/action-groups/stream")
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


@MOBILE_ONLY
async def test_consent_reconnect_warning_phone(reconnect_review: VisualPage) -> None:
    view = reconnect_review
    page = view.page
    await _in_viewport(page.locator("[data-reconnect-review]").get_by_text("Replace authorization", exact=False))
    await _in_viewport(page.get_by_text(re.compile("I confirm replacing this Connection")))
    await view.capture(target=view.page.locator("#app"))


@MOBILE_ONLY
async def test_consent_reconnect_decision_phone(reconnect_review: VisualPage) -> None:
    view = reconnect_review
    page = view.page
    authorize = page.get_by_role("button", name="Authorize")
    await _focus(page, authorize)
    await _in_viewport(page.get_by_role("button", name="Deny"))
    await expect(authorize).to_be_disabled()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_policy_set_selector_hides_picked_option(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes?preset=public-coder")
    await view.page.wait_for_selector('.mantine-Pill-root:has-text("github-public")', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Policy sets", available="pypi", picked="github-public", press_arrow_down=True)
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
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
