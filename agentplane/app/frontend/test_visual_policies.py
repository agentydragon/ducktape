"""Agentplane policies visual behavior tests."""

import re

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.visual_app import IDLE_THREAD, AgentplaneFixture
from agentplane.app.frontend.visual_assertions import (
    _focus,
    _in_viewport,
    _open_raw_switches,
    _open_select,
    _select_reconnect,
)
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
# gazelle:include_dep //agentplane/app/frontend:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_actions_sidebar_queue(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.show_pending_actions()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    if viewport == MOBILE:
        await page.get_by_role("button", name="Toggle navigation").click()
    section = page.locator(".agentplane-actions-sidebar")
    await expect(section.locator(".agentplane-actions-sidebar-toggle")).to_have_attribute("aria-expanded", "true")
    await expect(section.get_by_text("restart the test backup service")).to_be_visible()
    await expect(page.get_by_label("Message")).to_have_count(1)
    await view.capture()


async def test_actions_sidebar_resize(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
async def test_sidebar_compact_pod_approval(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport, expanded: bool
) -> None:
    await app.show_compact_pod_action()
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    if viewport == MOBILE:
        await page.get_by_role("button", name="Toggle navigation").click()
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
        await expect(section.get_by_role("button", name="Approve inspect running demo pods")).to_have_count(0)
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("group", "name", "arguments", "visible"),
    [
        (
            "kubernetes_admin",
            "resources_get",
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
            "Deployment",
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
            "Delete resource",
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
    ],
    ids=[
        "resource-get",
        "pods-in-namespace",
        "resource-list",
        "pod-log",
        "resource-delete",
        "events-list",
        "github-pr",
    ],
)
async def test_compact_action_chips(
    view: VisualPage,
    app: AgentplaneFixture,
    viewport: Viewport,
    group: str,
    name: str,
    arguments: dict[str, object],
    visible: str,
) -> None:
    await app.show_action_preview(group, name, arguments)
    await app.mount_thread(IDLE_THREAD)
    if viewport == MOBILE:
        await view.page.get_by_role("button", name="Toggle navigation").click()
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
    await expect(section.get_by_role("link", name=f"View details for {title}")).to_be_visible()
    if group == "kubernetes_admin" and name in {"resources_get", "pods_list_in_namespace", "pods_log"}:
        await expect(section.get_by_role("button", name=f"Approve {title}")).to_be_visible()
    if group == "kubernetes_admin" and name in {"resources_get", "pods_log"}:
        await expect(section.get_by_text("namespace: (not specified)", exact=True)).to_be_visible()
    if group == "kubernetes_admin" and name == "pods_log":
        await expect(section.get_by_text("container: (not specified)", exact=True)).to_be_visible()
    await view.capture(target=section)


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_compact_pr_with_description_requires_review(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
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
    if viewport == MOBILE:
        await view.page.get_by_role("button", name="Toggle navigation").click()
    await section.get_by_role("button", name="Expand review create pull request").click()
    await expect(section.get_by_text("description: open Review")).to_be_visible()
    await expect(section.get_by_role("button", name="Approve review create pull request")).to_have_count(0)
    await expect(section.get_by_role("link", name="View details for review create pull request")).to_be_visible()
    await view.capture(target=section)


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_wide_action_decisions(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.show_pending_actions()
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_action_detail_ssh_raw_view(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.show_pending_actions()
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


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_action_detail_external_grant_audit(view: VisualPage, app: AgentplaneFixture, viewport: Viewport) -> None:
    await app.show_pending_actions()
    await app.mount_app("/actions/70000000-0000-4000-8000-000000000001")
    await view.check(context="fixture ready")
    page = view.page
    audit = page.get_by_text("Request & grant audit details")
    await expect(audit).to_be_visible()
    await audit.click()
    await expect(page.get_by_text("test-external-client")).to_be_visible()
    await view.capture(target=page.locator("#app"))


async def test_sidebar_long_ssh_preview_is_bounded_desktop(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_pending_actions()
    await app.long_pending_action()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    section = page.locator(".agentplane-actions-sidebar")
    await section.get_by_role("button", name="Expand restart the test backup service").click()
    preview = section.locator(".agentplane-actions-sidebar-preview").last
    await expect(preview.get_by_role("button", name="Show all 55 lines")).to_be_visible()
    await preview.get_by_role("button", name="Show all 55 lines").click()
    await preview.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await preview.evaluate("element => element.scrollTop") > 0
    await view.capture(target=section)


@pytest.mark.parametrize("viewport", [MOBILE], ids=["mobile"])
async def test_sidebar_long_ssh_preview_is_bounded_phone(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.show_pending_actions()
    await app.long_pending_action()
    await app.mount_thread(IDLE_THREAD)
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Toggle navigation").click()
    section = page.locator(".agentplane-actions-sidebar")
    await section.get_by_role("button", name="Expand restart the test backup service").click()
    preview = section.locator(".agentplane-actions-sidebar-preview").last
    await expect(preview.get_by_role("button", name="Show all 55 lines")).to_be_visible()
    await preview.get_by_role("button", name="Show all 55 lines").click()
    await preview.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await preview.evaluate("element => element.scrollTop") > 0
    await view.capture(target=section)


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
async def test_external_caller_grants(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    if page.viewport_size and page.viewport_size["width"] < 600:
        await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Settings").click()
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
    row = page.locator("[data-connection-id]").filter(has_text="Claude desktop")
    await expect(row.get_by_role("button", name="Confirm change")).to_be_visible()
    await expect(row.get_by_role("button", name="Apply")).to_have_count(0)
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        await confirm.scroll_into_view_if_needed()
    await view.capture()


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "mobile"])
async def test_connection_unlink_confirmation(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    if page.viewport_size and page.viewport_size["width"] < 600:
        await page.get_by_role("button", name="Toggle navigation").click()
    await page.get_by_role("button", name="Settings").click()
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
