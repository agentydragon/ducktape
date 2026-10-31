"""Agentplane policies visual behavior tests."""

import re

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import Locator, Page, expect

from agentplane.app.frontend.testing.app import BOUND_CONNECTION, IDLE_THREAD, AgentplaneFixture, Flag, Scene
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
from util.testing.page_capture import wait_for_stable
from util.testing.viewports import DESKTOP, MOBILE, Viewport
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

# Actions whose closed compact card already shows every argument of the calls below, so it has no expanded version.
CLOSED_CARD_SHOWS_EVERYTHING = frozenset(
    {
        ("kubernetes_admin", "pods_list_in_namespace"),
        ("kubernetes_admin", "pods_get"),
        ("kubernetes_admin", "pods_log"),
        ("kubernetes_admin", "events_list"),
        ("sandbox", "create"),
        ("sandbox", "get"),
        ("sandbox", "dispose"),
        ("sandbox", "list"),
        ("tana", "get_or_create_calendar_node"),
    }
)


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


async def _close_navigation_for_strip(page: Page, viewport: Viewport) -> Locator:
    """Close the desktop sidebar (phones start without it) so the attention strip carries the queue."""
    if viewport == DESKTOP:
        await page.get_by_role("button", name="Toggle navigation").click()
    await expect(page.get_by_role("navigation")).to_be_visible(visible=False)
    return page.locator(".agentplane-actions-attention")


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
async def test_actions_attention_strip_over_thread(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport, expanded: bool
) -> None:
    """A queue longer than the strip shows: the first two cards, and the rest behind "Show 2 more"."""
    await app.set_flag(Flag.PENDING_ACTION_OVERFLOW)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    strip = await _close_navigation_for_strip(page, viewport)
    await expect(strip.get_by_role("button", name="Approve inspect running demo pods")).to_be_visible()
    await expect(strip.get_by_role("button", name="Approve read the demo config")).to_be_visible()
    hidden = strip.get_by_text(re.compile("^(echo the test repository handle back|restart the test backup service)$"))
    await expect(hidden).to_have_count(0)
    if expanded:
        await strip.get_by_role("button", name="Show 2 more").click()
        await expect(hidden).to_have_count(2)
        await expect(strip.get_by_role("button", name="Show fewer")).to_have_attribute("aria-expanded", "true")
    # The clicks leave the pointer over whatever moved under it: a card or the top bar's buttons.
    await page.mouse.move(0, 0)
    await expect(page.get_by_label("Message")).to_be_visible()
    await view.check(context="Actions strip over the open thread")
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_actions_attention_strip_keeps_expansion_after_decision(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    """Approving on a details page opened from the expanded strip returns to the thread still expanded."""
    await app.set_flag(Flag.PENDING_ACTION_OVERFLOW)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    strip = await _close_navigation_for_strip(page, viewport)
    await strip.get_by_role("button", name="Show 2 more").click()
    await strip.get_by_role("link", name="View details for echo the test repository handle back").click()
    await page.get_by_role("button", name="Approve", exact=True).click()
    await expect(page.get_by_label("Message")).to_be_visible()
    await expect(strip.get_by_role("button", name="Show fewer")).to_have_attribute("aria-expanded", "true")
    await expect(strip.get_by_text("restart the test backup service")).to_be_visible()
    await expect(strip.get_by_text("echo the test repository handle back")).to_have_count(0)
    await page.mouse.move(0, 0)
    await view.check(context="back on the thread after the decision")
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
    sidebar = page.get_by_role("navigation")
    if await sidebar.is_visible() != sidebar_open:
        await page.get_by_role("button", name="Toggle navigation").click()
    await expect(sidebar).to_be_visible(visible=sidebar_open)
    strip = page.locator(".agentplane-actions-attention")
    await expect(strip).to_have_count(0 if sidebar_open or route == "/actions" else 1)
    await expect(page.locator(".agentplane-actions-sidebar")).to_have_count(0 if route == "/actions" else 1)
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
async def test_sidebar_compact_pod_approval(view: VisualPage, app: AgentplaneFixture) -> None:
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
    await expect(section.get_by_role("button", name="Expand inspect running demo pods")).to_have_count(0)
    summary = section.locator(".agentplane-actions-sidebar-collapsed-preview")
    await expect(summary.get_by_text("Matching pods where")).to_be_visible()
    await expect(summary.get_by_text("app=demo")).to_be_visible()
    await expect(summary.get_by_text("status.phase=Running")).to_be_visible()
    await expect(summary.get_by_text("test-apps")).to_have_count(0)
    await expect(section.get_by_text("test-apps", exact=True)).to_have_count(1)
    await expect(section.get_by_text("Requested by", exact=False)).to_have_count(0)
    await expect(section.get_by_text("Check the exact call before approving.", exact=True)).to_have_count(0)
    await expect(section.get_by_role("button", name="Approve inspect running demo pods")).to_be_visible()
    await expect(section.get_by_role("button", name="Deny inspect running demo pods")).to_be_visible()
    await view.capture(target=section)


ARGUMENT_LAYOUTS = [
    pytest.param(
        "kubernetes_admin",
        "pods_list_in_namespace",
        {
            "namespace": "test-namespace-" + "n" * 48,
            "labelSelector": "app=" + "demo-" + "d" * 56,
            "fieldSelector": "status.phase=Running," + "x" * 48,
        },
        ["test-namespace-", "label selector", "status.phase=Running"],
        "Matching pods where",
        id="long-target-and-selectors",
    ),
    pytest.param(
        "gmail",
        "threads_list",
        {"q": "from:deployments@example.com " + "query-" * 12, "maxResults": 23},
        ['"maxResults": 23', "from:deployments@example.com"],
        "from:deployments@example.com",
        id="gmail-query-limit",
    ),
    pytest.param(
        "kubernetes_admin",
        "pods_log",
        {
            "name": "api-" + "pod-" * 12,
            "namespace": "apps",
            "container": "sidecar-" + "container-" * 8,
            "previous": True,
            "tail": -1,
        },
        ["container sidecar-", "previous run", "tail -1"],
        "previous run",
        id="previous-pod-logs",
    ),
    pytest.param(
        "kubernetes_admin",
        "resources_list",
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "namespace": "apps",
            "fieldSelector": "status.phase=Running," + "x" * 48,
            "labelSelector": "app=" + "demo-" + "d" * 56,
        },
        ["field selector status.phase=Running", "label selector app=demo-"],
        "Matching resources where",
        id="resource-list-selectors",
    ),
    pytest.param(
        "kubernetes_admin",
        "events_list",
        {"namespace": "apps", "fieldSelector": "type=Warning," + "reason-" * 8},
        ["Show events matching", "type=Warning"],
        "Show events matching",
        id="event-field-selector",
    ),
    pytest.param(
        "kubernetes_admin",
        "resources_delete",
        {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps", "gracePeriodSeconds": 0},
        ["Use API version v1", "request a 0s grace period"],
        "Request a 0s grace period",
        id="resource-delete-grace-period",
    ),
    pytest.param(
        "tana",
        "get_or_create_calendar_node",
        {"workspaceId": "workspace-123", "granularity": "week", "date": "2026-10-12"},
        ["week calendar node", "2026-10-12", "workspace-123"],
        "week calendar node",
        id="calendar-context",
    ),
]


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize(
    ("group", "name", "arguments", "visible", "overview", "expanded"),
    [
        pytest.param(*layout.values, expanded, id=f"{layout.id}-{'expanded' if expanded else 'collapsed'}")
        for layout in ARGUMENT_LAYOUTS
        for expanded in ((False,) if tuple(layout.values[:2]) in CLOSED_CARD_SHOWS_EVERYTHING else (False, True))
    ],
)
async def test_action_argument_layouts(
    view: VisualPage,
    app: AgentplaneFixture,
    expanded: bool,
    group: str,
    name: str,
    arguments: dict[str, object],
    visible: list[str],
    overview: str,
) -> None:
    await app.show_action_preview(group, name, arguments)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    await _open_phone_navigation(page)
    section = page.locator(".agentplane-actions-sidebar")
    title = "inspect running demo pods" if name == "pods_list_in_namespace" else f"review {name.replace('_', ' ')}"
    disclosure = section.get_by_role("button", name=f"Expand {title}")
    if expanded:
        await disclosure.click()
        for value in visible:
            await expect(section.get_by_text(value, exact=False)).to_be_visible()
        await expect(section.get_by_role("button", name=f"Approve {title}")).to_be_visible()
        await expect(section.get_by_role("button", name=f"Deny {title}")).to_be_visible()
    else:
        await expect(section.get_by_text(overview, exact=False)).to_be_visible()
        if (group, name) in CLOSED_CARD_SHOWS_EVERYTHING:
            await expect(disclosure).to_have_count(0)
            for value in visible:
                await expect(section.get_by_text(value, exact=False)).to_be_visible()
        if (group, name) != ("tana", "get_or_create_calendar_node"):
            await expect(section.get_by_role("button", name=f"Approve {title}")).to_be_visible()
            await expect(section.get_by_role("button", name=f"Deny {title}")).to_be_visible()
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize(
    ("name", "arguments", "result", "visible"),
    [
        (
            "exec",
            {"name": "timed-out-sandbox", "script": 'echo "$HOME"', "timeout_seconds": 1, "max_output_bytes": 32},
            {
                "exit": {"kind": "timed_out"},
                "stdout": {"truncated_text": "partial output", "total_bytes": 256},
                "stderr": "deadline reached",
                "duration_seconds": 1.01,
            },
            "Timed out",
        ),
        (
            "get",
            {"name": "waiting-sandbox"},
            {
                "name": "waiting-sandbox",
                "template": "python",
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "ContainersNotReady",
                        "message": "Waiting for container startup",
                    }
                ],
                "node_name": "worker-1",
                "pod_ips": ["10.0.0.1"],
            },
            "ContainersNotReady",
        ),
        ("list", {}, {"sandboxes": []}, "No sandboxes."),
        ("dispose", {"name": "absent-sandbox"}, {"name": "absent-sandbox", "existed": False}, "already absent"),
    ],
    ids=["exec-timeout-truncated", "get-not-ready", "list-empty", "dispose-absent"],
)
async def test_sandbox_action_results(
    view: VisualPage,
    app: AgentplaneFixture,
    name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    visible: str,
) -> None:
    await app.show_action_result(name, arguments, result)
    await app.mount_app("/actions/70000000-0000-4000-8000-000000000007")
    await expect(view.page.get_by_text(visible, exact=False)).to_be_visible()
    await view.capture()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize(
    ("group", "name", "arguments", "visible"),
    [
        pytest.param(
            "kubernetes_admin",
            "resources_get",
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web"},
            "Read with API version apps/v1",
            id="resource-get",
        ),
        pytest.param(
            "kubernetes_admin",
            "pods_list_in_namespace",
            {"namespace": "apps", "labelSelector": "app=web", "fieldSelector": "status.phase=Running"},
            "status.phase=Running",
            id="pods-in-namespace",
        ),
        pytest.param(
            "kubernetes_admin",
            "resources_list",
            {"apiVersion": "v1", "kind": "Pod", "namespace": "apps", "labelSelector": "app=web"},
            "app=web",
            id="resource-list",
        ),
        pytest.param(
            "kubernetes_admin", "pods_get", {"name": "web-0", "namespace": "apps"}, "Get Pod apps/web-0", id="pod-get"
        ),
        pytest.param(
            "kubernetes_admin",
            "pods_log",
            {"name": "web-0", "previous": True, "tail": -1},
            "previous run, tail -1",
            id="pod-log",
        ),
        pytest.param(
            "kubernetes_admin",
            "resources_delete",
            {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps", "gracePeriodSeconds": 0},
            "request a 0s grace period",
            id="resource-delete",
        ),
        pytest.param(
            "kubernetes_admin",
            "events_list",
            {"namespace": "apps", "fieldSelector": "type=Warning"},
            "type=Warning",
            id="events-list",
        ),
        pytest.param(
            "github",
            "create_pull_request",
            {
                "owner": "example",
                "repo": "repo",
                "title": "Update docs",
                "head": "docs",
                "base": "devel",
                "draft": True,
                "maintainer_can_modify": False,
                "reviewers": ["reviewer1", "reviewer2"],
            },
            "Update docs",
            id="github-pr",
        ),
        pytest.param(
            "kubernetes_admin",
            "pods_exec",
            {"name": "web-0", "namespace": "apps", "container": "main", "command": ["printenv", "POD_NAME"]},
            "printenv POD_NAME",
            id="pod-exec",
        ),
        pytest.param(
            "gmail",
            "drafts_create",
            {
                "to": ["reviewer@example.com", "owner@example.com"],
                "cc": ["team@example.com"],
                "subject": "Deployment review",
                "body": "Please review.\nConfirm the rollout.\nCheck the deployment notes.\nReply with any blockers.",
                "thread_id": "existing-thread-123",
            },
            "reviewer@example.com",
            id="gmail-draft",
        ),
        pytest.param(
            "gmail",
            "threads_list",
            {"q": "from:deployments@example.com", "maxResults": 5},
            "from:deployments@example.com",
            id="gmail-search",
        ),
        pytest.param(
            "sandbox",
            "exec",
            {
                "name": "script-sandbox",
                "script": 'for f in *.txt; do\n  printf "%s\\n" "$f"\ndone',
                "timeout_seconds": 60,
                "max_output_bytes": 4096,
                "cwd": "/workspace",
            },
            "Working directory /workspace",
            id="sandbox-exec",
        ),
        pytest.param("sandbox", "create", {"name": "new-sandbox", "template": "python"}, "python", id="sandbox-create"),
        pytest.param(
            "sandbox", "get", {"name": "inspected-sandbox"}, "Inspect sandbox inspected-sandbox", id="sandbox-get"
        ),
        pytest.param(
            "sandbox", "dispose", {"name": "disposed-sandbox"}, "Dispose sandbox disposed-sandbox", id="sandbox-dispose"
        ),
        pytest.param("sandbox", "list", {}, "List sandboxes", id="sandbox-list"),
        pytest.param("grocy_sf", "products_list", {"detail": "full"}, "Full Product records", id="grocy-products"),
        pytest.param("grocy_sf", "quantity_units_list", {}, "Quantity unit names", id="grocy-units"),
        pytest.param(
            "tana",
            "get_or_create_calendar_node",
            {"workspaceId": "workspace-123", "granularity": "week", "date": "2026-10-12"},
            "week calendar node",
            id="tana-calendar-node",
        ),
    ],
)
async def test_compact_action_summaries(
    view: VisualPage,
    app: AgentplaneFixture,
    capture_name: str,
    group: str,
    name: str,
    arguments: dict[str, object],
    visible: str,
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
    collapsed_summary = section.locator(".agentplane-actions-sidebar-collapsed-preview")
    if group == "gmail" and name == "drafts_create":
        await expect(collapsed_summary.get_by_text("Deployment review", exact=True)).to_be_visible()
        await expect(collapsed_summary.get_by_text("Cc team@example.com", exact=True)).to_be_visible()
        await expect(
            collapsed_summary.get_by_text("Replying to thread existing-thread-123", exact=True)
        ).to_be_visible()
        await view.capture(f"{capture_name}-collapsed", target=section)
    if group == "github" and name == "create_pull_request":
        for chip in ("draft", "maintainers cannot edit", "review from reviewer1, reviewer2"):
            await expect(collapsed_summary.get_by_text(chip, exact=True)).to_be_visible()
        await view.capture(f"{capture_name}-collapsed", target=section)
    if (group, name) in CLOSED_CARD_SHOWS_EVERYTHING:
        await expect(disclosure).to_have_count(0)
    else:
        bounds = await disclosure.bounding_box()
        assert bounds is not None
        await disclosure.click(position={"x": bounds["width"] - 4, "y": bounds["height"] - 3})
    await expect(section.get_by_text(visible)).to_be_visible()
    if group == "kubernetes_admin" and name == "pods_list_in_namespace":
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text("List pods in namespace")
    if group == "gmail" and name == "threads_list":
        await expect(section.locator(".agentplane-actions-sidebar-name")).to_contain_text("Gmail: Search threads")
        await expect(
            section.locator(".agentplane-actions-sidebar-preview").get_by_text('"maxResults": 5', exact=False)
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
    if (group, name) in {
        ("kubernetes_admin", "resources_get"),
        ("kubernetes_admin", "pods_list_in_namespace"),
        ("kubernetes_admin", "pods_get"),
        ("kubernetes_admin", "pods_log"),
        ("kubernetes_admin", "resources_delete"),
        ("sandbox", "get"),
        ("sandbox", "dispose"),
        ("sandbox", "list"),
        ("sandbox", "create"),
    }:
        await expect(section.get_by_role("button", name=f"Approve {title}")).to_be_visible()
        await expect(section.get_by_role("button", name=f"Deny {title}")).to_be_visible()
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
@pytest.mark.parametrize(
    ("group", "name", "arguments", "token"),
    [
        pytest.param(
            "ssh",
            "exec",
            {
                "host": "archive.example.com",
                "user": "backup",
                "command": "for target in mirror-a mirror-b mirror-c; do rsync --archive --delete --compress "
                "--exclude='*.tmp' --exclude='.cache' /srv/backups/ backup@$target.example.com:/srv/mirror/ "
                '&& echo "synced $target" || echo "failed $target" >&2; done; '
                "tar --create --zstd --file /srv/archive/backups-$(date +%F).tar.zst /srv/backups",
            },
            "keyword",
            id="ssh-exec",
        ),
        pytest.param(
            "kubernetes_admin",
            "pods_exec",
            {
                "name": "web-0",
                "namespace": "apps",
                "command": [
                    "sh",
                    "-c",
                    'for log in /var/log/app/*.log; do gzip --keep --force "$log" && echo "compressed $log"; done; '
                    "find /var/log/app -name '*.gz' -mtime +14 -print -delete; df --human-readable /var/log/app",
                ],
            },
            "string",
            id="pod-exec",
        ),
    ],
)
async def test_compact_exec_long_command(
    view: VisualPage,
    app: AgentplaneFixture,
    capture_name: str,
    group: str,
    name: str,
    arguments: dict[str, object],
    token: str,
    expanded: bool,
) -> None:
    await app.show_action_preview(group, name, arguments)
    await app.mount_thread(IDLE_THREAD)
    page = view.page
    await _open_phone_navigation(page)
    section = page.locator(".agentplane-actions-sidebar")
    title = f"review {name.replace('_', ' ')}"
    if expanded:
        await section.get_by_role("button", name=f"Expand {title}").click()
        await expect(section.locator(".agentplane-actions-sidebar-preview")).to_be_visible()
        await expect(section.locator(".ui-code-block-placeholder")).to_have_count(0)
    else:
        summary = section.locator(".agentplane-actions-sidebar-collapsed-preview")
        await expect(summary.locator(f".ui-tok-{token}").first).to_be_visible()
        # However narrow the card, a command takes three lines and the rest is cut off. A chip makes
        # its own line a little taller, so a whole fourth line is what this must not reach.
        measured = await summary.evaluate(
            """row => {
                const text = row.firstElementChild;
                return {
                    height: text.getBoundingClientRect().height,
                    lineHeight: parseFloat(getComputedStyle(text).lineHeight),
                    clipped: text.scrollHeight > text.clientHeight,
                };
            }"""
        )
        assert measured["clipped"]
        assert measured["height"] < 4 * measured["lineHeight"]
    await wait_for_stable(page)
    await view.capture(target=section)


@pytest.mark.parametrize(
    ("group", "name"),
    [("ssh", "list_targets"), ("uncatalogued", "unknown_action"), ("kubernetes_admin", "resources_get")],
    ids=["unregistered-action", "unknown-action", "schema-mismatch"],
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
    await expect(preview.get_by_text("Details shown as recorded data", exact=True)).to_be_visible()
    await expect(section.get_by_role("button", name=f"Approve {title}")).to_have_count(0)
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
async def test_compact_pr_with_description_offers_card_decisions(
    view: VisualPage, app: AgentplaneFixture, capture_name: str
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
            "body": "Important PR description visible on the compact card.\n"
            "Second paragraph explains the rollout and its verification.\n"
            "Final paragraph lists the remaining review questions.",
        },
    )
    await app.mount_thread(IDLE_THREAD)
    section = view.page.locator(".agentplane-actions-sidebar")
    await _open_phone_navigation(view.page)
    collapsed_summary = section.locator(".agentplane-actions-sidebar-collapsed-preview")
    await expect(
        collapsed_summary.get_by_text("Important PR description visible on the compact card.", exact=False)
    ).to_be_visible()
    await expect(section.get_by_role("button", name="Approve review create pull request")).to_be_visible()
    await expect(section.get_by_role("button", name="Deny review create pull request")).to_be_visible()
    await view.capture(f"{capture_name}-collapsed", target=section)
    await section.get_by_role("button", name="Expand review create pull request").click()
    await expect(
        section.get_by_text("Important PR description visible on the compact card.", exact=False)
    ).to_be_visible()
    await expect(section.get_by_role("button", name="Approve review create pull request")).to_be_visible()
    await expect(section.get_by_role("button", name="Deny review create pull request")).to_be_visible()
    await expect(section.get_by_role("link", name="View details for review create pull request")).to_be_visible()
    await view.capture(target=section)


@DESKTOP_AND_MOBILE
async def test_compact_pull_request_search_offers_card_decisions(
    view: VisualPage, app: AgentplaneFixture, capture_name: str
) -> None:
    await app.show_action_preview(
        "github",
        "search_pull_requests",
        {
            "owner": "example",
            "repo": "repo",
            "query": "is:open label:bug",
            "page": 2,
            "perPage": 10,
            "sort": "updated",
            "order": "desc",
            "fields": ["number", "title", "state", "html_url"],
        },
    )
    await app.mount_thread(IDLE_THREAD)
    section = view.page.locator(".agentplane-actions-sidebar")
    await _open_phone_navigation(view.page)
    collapsed_summary = section.locator(".agentplane-actions-sidebar-collapsed-preview")
    await expect(collapsed_summary).to_contain_text("is:open label:bug")
    await expect(collapsed_summary).to_contain_text("example")
    await expect(collapsed_summary).to_contain_text("repo")
    await expect(collapsed_summary).not_to_contain_text("Page 2")
    await expect(collapsed_summary).not_to_contain_text("results per page")
    await expect(collapsed_summary).not_to_contain_text("Sort by")
    await expect(collapsed_summary).not_to_contain_text("Order:")
    await expect(collapsed_summary).not_to_contain_text("Fields:")
    await expect(section.get_by_role("button", name="Approve review search pull requests")).to_be_visible()
    await expect(section.get_by_role("button", name="Deny review search pull requests")).to_be_visible()
    await expect(section.get_by_role("button", name="Expand review search pull requests")).to_have_count(0)
    await view.capture(f"{capture_name}-collapsed", target=section)


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
    await expect(preview.locator(".agentplane-command-heading")).to_be_attached()
    await expect(preview.get_by_role("button", name="Show less Command")).to_be_visible()
    await preview.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await wait_for_stable(page)
    assert await preview.evaluate("element => element.scrollTop") > 0
    # The short sidebar preview scrolls independently. Its command label must scroll with the
    # text, leaving no pinned disclosure row for the final lines to peek above.
    heading = preview.locator(".agentplane-command-heading")
    preview_box = await preview.bounding_box()
    heading_box = await heading.bounding_box()
    assert preview_box is not None
    assert heading_box is not None
    assert heading_box["y"] + heading_box["height"] <= preview_box["y"] + 1
    await expect(preview.get_by_role("button", name="Show less Command")).to_be_in_viewport()
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
    await expect(page.locator(".ui-code-block").first).to_be_visible()
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_external_caller_grants(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    await _open_settings(page)
    await page.get_by_role("button", name="View grants", exact=True).first.click()
    grants = page.get_by_role("region", name="Grants for agentplane-test/personal")
    await expect(grants.get_by_role("heading", name="Bindings", exact=True)).to_be_visible()
    await expect(grants.get_by_role("heading", name="Auto-approve if", exact=True)).to_be_visible()
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
    row = page.locator(f'[data-connection-id="{BOUND_CONNECTION}"]')
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
    row = page.locator(f'[data-connection-id="{BOUND_CONNECTION}"]')
    await row.get_by_role("button", name="Unlink", exact=True).click()
    confirm = row.get_by_role("button", name="Confirm unlink")
    await expect(confirm).to_be_visible()
    await expect(row.get_by_text("Already claimed executions are not stopped", exact=False)).to_be_visible()
    await expect(row.get_by_role("button", name="Unlink", exact=True)).to_have_count(0)
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
        await confirm.scroll_into_view_if_needed()
    await view.capture()


@DESKTOP_AND_MOBILE
async def test_connection_rename_editing(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    await view.check(context="fixture ready")
    page = view.page
    await _open_settings(page)
    name = page.locator(f'[data-connection-id="{BOUND_CONNECTION}"]').get_by_role("textbox", name="Client name")
    await expect(name).to_have_value("Claude desktop")
    await name.fill("Name being typed")
    await expect(name).to_be_focused()
    if page.viewport_size and page.viewport_size["width"] < 600:
        assert await page.locator(".mantine-Modal-content").evaluate("el => el.scrollWidth <= el.clientWidth")
    await view.capture()


async def test_oauth_clients_update_while_settings_stays_open(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/")
    page = view.page
    await _open_settings(page)
    name = page.locator(f'[data-connection-id="{BOUND_CONNECTION}"]').get_by_role("textbox", name="Client name")
    await expect(name).to_have_value("Claude desktop")
    await app.publish(Flag.CONNECTION_RENAMED, "/upstream/connections/v1/operator/connections:watch")
    await expect(name).to_have_value("Updated OAuth client")
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


async def _open_history_receipts(page: Page) -> None:
    await expect(page.get_by_role("button", name="Receipt details", exact=True).first).to_be_attached()
    for control in await page.get_by_role("button", name="Receipt details", exact=True).all():
        if await control.get_attribute("aria-expanded") == "false":
            await control.click()


@DESKTOP_AND_MOBILE
async def test_action_history_summaries(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    page = view.page
    summary = page.get_by_text("render the test diagram", exact=True)
    card = page.locator('[data-action-receipt="70000000-0000-4000-8000-000000000002"]')
    await expect(card.get_by_role("button", name="Receipt details", exact=True)).to_have_attribute(
        "aria-expanded", "false"
    )
    await expect(page.get_by_role("link", name="View details for render the test diagram")).to_be_visible()
    await _focus(page, summary)
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_history_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    await view.page.wait_for_selector(".ui-disclosure-summary", state="attached")
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
    card = page.locator('[data-action-receipt="70000000-0000-4000-8000-000000000010"]')
    overview = card.locator(".agentplane-action-history-compact-overview")
    await expect(overview).to_be_visible()
    await expect(overview.get_by_text("The release is ready.", exact=False)).to_have_count(0)
    await expect(card.get_by_role("button", name="Receipt details", exact=True)).to_have_attribute(
        "aria-expanded", "false"
    )
    await _in_viewport(card)
    await view.check(context="Gmail draft compact history card")
    await view.capture("gmail_draft_history_compact", target=card)

    await _open_history_receipts(page)
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
    await _open_history_receipts(view.page)
    await view.page.wait_for_selector(".ui-disclosure-summary", state="attached")
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


@DESKTOP_AND_MOBILE
async def test_action_history_automatic_paging(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.set_flag(Flag.PAGED_ACTION_HISTORY)
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    page = view.page
    await expect(page.get_by_test_id("action-history-sentinel")).to_be_attached()
    await page.locator(".agentplane-shell-main").evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await expect(page.get_by_text("End of action history.", exact=True)).to_be_attached()
    last_receipt = page.locator("[data-action-receipt]").last
    await last_receipt.scroll_into_view_if_needed()
    await wait_for_stable(page)
    result_sections = last_receipt.get_by_test_id("action-execution-outcome").locator(".agentplane-operation-section")
    await expect(result_sections).to_have_count(1)
    result_section = result_sections.first
    await result_section.scroll_into_view_if_needed()
    await expect(result_section.locator(".ui-code-block-placeholder")).to_have_count(0)
    await expect(result_section.locator(".ui-code-block-container .cm-editor")).to_be_visible()
    await wait_for_stable(page)
    await _focus(page, page.get_by_text("End of action history.", exact=True))
    await view.capture(target=page.locator("#app"))


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
    await app.publish(Flag.MCP_LINKAGE_CHANGED, "/upstream/actions/v1/operator/mcp-servers:watch")
    await expect(docs.get_by_text("expired", exact=True)).to_be_visible()
    await app.publish(Flag.MCP_HEALTH_CHANGED, "/upstream/actions/v1/action-groups:watch")
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
    await app.mount_app("/sandboxes?preset=test-coder")
    await view.page.wait_for_selector('.mantine-Pill-root:has-text("test-basic")', state="attached")
    await view.check(context="fixture ready")
    page = view.page
    await _open_select(page, label="Policy sets", available="pypi", picked="test-basic", press_arrow_down=True)
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_form_test_coder_preset_layout(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes?preset=test-coder")
    await view.page.wait_for_selector('.mantine-Pill-root:has-text("test-pr-failed-jobs")', state="attached")
    await expect(view.page.locator(".mantine-Pill-root")).to_have_count(14)
    headers = view.page.locator("thead tr th")
    await expect(headers.nth(0)).to_have_text("Status")
    await expect(headers.nth(1)).to_have_text("Name")
    rows = view.page.locator("tbody tr")
    await expect(rows).to_have_count(3)
    for row in await rows.all():
        await expect(row.locator("td").nth(0).locator(".agentplane-mark")).to_have_count(1)
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_sandbox_policy_grant_pick_updates_options(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.mount_app("/sandboxes/ready-sandbox?tab=policy&rules=ready-sandbox-github-public")
    await view.check(context="fixture ready")
    page = view.page
    await page.get_by_role("button", name="Grant policy sets", exact=True).click()
    await _open_select(page, label="Policy sets", available="pypi")
    await page.get_by_role("option", name=re.compile("pypi")).click()
    await expect(page.locator(".mantine-Pill-root", has_text="pypi")).to_be_visible()
    await expect(page.get_by_role("option", name=re.compile("github-public"))).to_be_visible()
    await expect(page.get_by_role("option", name=re.compile("pypi"))).to_have_count(0)
    await page.mouse.move(0, 0)
    await expect(page.get_by_role("tooltip")).to_have_count(0)
    await wait_for_stable(page)
    await view.capture(target=view.page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_action_history_filter(view: VisualPage, app: AgentplaneFixture, capture_name: str) -> None:
    await app.set_flag(Flag.PENDING_ACTIONS)
    await app.mount_app("/actions")
    page = view.page
    filtered = page.get_by_role("radio", name="Not auto-allowed", exact=True)
    all_actions = page.get_by_role("radio", name="All actions", exact=True)
    auto_allowed = page.get_by_text("search ducktape for auto_allow", exact=True)
    await expect(filtered).to_be_checked()
    await expect(page.get_by_text("list the test backup archive", exact=True)).to_be_visible()
    await expect(auto_allowed).to_have_count(0)
    await _focus(page, page.get_by_text("Not auto-allowed", exact=True))
    await view.capture()
    await page.get_by_text("All actions", exact=True).click()
    await expect(all_actions).to_be_checked()
    await expect(auto_allowed).to_be_visible()
    await _focus(page, page.get_by_text("All actions", exact=True))
    await view.capture(f"{capture_name}-all-actions")
    await page.get_by_text("Not auto-allowed", exact=True).click()
    await expect(auto_allowed).to_have_count(0)


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("kind", ["text", "json", "email", "manifest"])
async def test_long_action_text(view: VisualPage, app: AgentplaneFixture, capture_name: str, kind: str) -> None:
    text = "\n".join(f"Long action content line {index + 1}" for index in range(48))
    group, name = "example_docs", "long_text"
    arguments: dict[str, object] = {"lines": text.splitlines()}
    result: object = {"content": [{"type": "text", "text": text}]}
    if kind == "json":
        result = {"content": [], "structuredContent": {"lines": text.splitlines()}}
    elif kind == "email":
        group, name = "gmail", "drafts_create"
        arguments = {"to": ["reader@example.com"], "subject": "Long email body", "body": text}
        result = {"content": [], "structuredContent": {"id": "long-draft"}}
    elif kind == "manifest":
        group, name = "kubernetes_admin", "resources_create_or_update"
        arguments = {
            "resource": "apiVersion: v1\nkind: ConfigMap\ndata:\n"
            + "\n".join(f"  line{index}: Long manifest content" for index in range(48))
        }
        result = {"content": []}
    await app.show_action_receipt(group, name, arguments, result)
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.get_by_text("Long action text", exact=True)).to_be_visible()
    if kind == "json":
        await expect(page.get_by_text("Structured content", exact=True)).to_be_visible()
    elif kind == "manifest":
        await expect(page.get_by_text("Apply ConfigMap", exact=True)).to_be_visible()

    if kind in {"text", "json"}:
        block = page.get_by_test_id("action-execution-outcome").locator(".ui-clamped-block")
        expand_label: str | re.Pattern[str] = "Show more"
        collapse_label: str | re.Pattern[str] = "Payload"
    elif kind == "email":
        block = page.locator(".ui-clamped-block:has([data-operation-heading])").filter(has_text="Message body")
        expand_label = "Show more"
        collapse_label = "Message body"
    else:
        block = page.locator(".ui-clamped-block:has([data-operation-heading])").filter(has_text="Manifest")
        expand_label = re.compile(r"Show all \d+ lines")
        collapse_label = re.compile(r"^Manifest(?:$| ·)")
    await expect(block).to_have_count(1)
    await expect(block).to_be_visible()
    await expect(block.locator('[data-clamped="true"]')).to_be_visible()
    await _focus(page, block)
    await view.capture()
    await block.get_by_role("button", name=expand_label, exact=isinstance(expand_label, str)).click()
    await expect(block).to_have_attribute("data-expanded", "true")
    await expect(block.locator('[data-clamped="true"]')).to_have_count(0)
    await view.capture(f"{capture_name}-expanded")
    await block.get_by_role("button", name=collapse_label, exact=isinstance(collapse_label, str)).click()
    await expect(block).to_have_attribute("data-expanded", "false")
    if kind == "email":
        await _open_raw_switches(page)
        await expect(page.get_by_text("Exact arguments", exact=True)).to_be_visible()
        await expect(page.locator('[data-clamped="true"]').first).to_be_visible()
        await view.capture(f"{capture_name}-raw")


@DESKTOP_AND_MOBILE
async def test_kubernetes_applied_manifest_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_action_receipt(
        "kubernetes_admin",
        "resources_create_or_update",
        {
            "resource": "{apiVersion: v1, kind: ConfigMap, metadata: {name: settings, namespace: apps}, data: {mode: safe}}"
        },
        {
            "content": [
                {
                    "type": "text",
                    "text": "- apiVersion: v1\n  kind: ConfigMap\n  metadata:\n    name: settings\n    namespace: apps\n  data:\n    mode: safe\n- apiVersion: v1\n  kind: Secret\n  metadata:\n    name: token\n    namespace: apps\n",
                }
            ],
            "isError": False,
        },
    )
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.get_by_text("Apply ConfigMap apps/settings", exact=True)).to_be_visible()
    await expect(page.locator('[data-operation-heading="Manifest · ConfigMap apps/settings"]:visible')).to_be_visible()
    await expect(
        page.locator('[data-operation-heading="Resource 1 · ConfigMap apps/settings"]:visible')
    ).to_be_visible()
    await expect(page.locator('[data-operation-heading="Resource 2 · Secret apps/token"]:visible')).to_be_visible()
    await expect(page.locator(".cm-editor").first).to_be_visible()
    await view.check(context="Kubernetes apply summary with formatted manifest and returned resources")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_kubernetes_yaml_result_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    resource = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "settings", "namespace": "apps"},
        "data": {"mode": "safe"},
    }
    await app.show_action_receipt(
        "kubernetes_admin",
        "resources_get",
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "settings", "namespace": "apps"},
        {
            "content": [
                {
                    "type": "text",
                    "text": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n  namespace: apps\ndata:\n  mode: safe",
                }
            ],
            "structuredContent": resource,
            "isError": False,
        },
    )
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.locator('[data-operation-heading="Resource · ConfigMap apps/settings"]:visible')).to_be_visible()
    await expect(page.get_by_text("Structured content", exact=True)).to_have_count(0)
    await view.check(context="Kubernetes YAML and its equivalent structured result shown once")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_kubernetes_delete_success_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_action_receipt(
        "kubernetes_admin",
        "resources_delete",
        {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps"},
        {"content": [{"type": "text", "text": "Resource deleted successfully"}], "isError": False},
    )
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.get_by_text("Delete Pod apps/web-0", exact=True)).to_be_visible()
    await expect(page.get_by_text("Deletion accepted", exact=True)).to_be_visible()
    await expect(page.get_by_text("Result", exact=True)).to_have_count(0)
    await view.check(context="successful Kubernetes delete shown beside its target")
    await view.capture(target=page.locator("#app"))


@DESKTOP_AND_MOBILE
async def test_kubernetes_pods_delete_success_receipt(
    view: VisualPage, app: AgentplaneFixture, viewport: Viewport
) -> None:
    await app.show_action_receipt(
        "kubernetes_admin",
        "pods_delete",
        {"name": "web-0", "namespace": "apps"},
        {"content": [{"type": "text", "text": "Pod deleted successfully"}], "isError": False},
    )
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.get_by_text("Delete Pod apps/web-0", exact=True)).to_be_visible()
    await expect(page.get_by_text("Deletion accepted", exact=True)).to_be_visible()
    await expect(page.get_by_text("Result", exact=True)).to_have_count(0)
    await expect(page.get_by_text("Exact arguments", exact=True)).to_have_count(0)
    await view.check(context="successful pod deletion shown as an accepted request")
    await view.capture(target=page.locator("#app"), name=f"pods_delete_pretty_{viewport.width}")

    await _open_raw_switches(page)
    await expect(page.get_by_text("Exact arguments", exact=True)).to_be_visible()
    await expect(page.get_by_text('"name": "web-0"', exact=False)).to_be_visible()
    await expect(page.get_by_text('"Pod deleted successfully"', exact=False)).to_be_visible()
    await view.capture(target=page.locator("#app"), name=f"pods_delete_raw_{viewport.width}")


@DESKTOP_AND_MOBILE
async def test_kubernetes_pods_delete_error_receipt(view: VisualPage, app: AgentplaneFixture) -> None:
    await app.show_action_receipt(
        "kubernetes_admin",
        "pods_delete",
        {"name": "web-0", "namespace": "apps"},
        {"content": [{"type": "text", "text": "Pod deleted successfully"}], "isError": True},
    )
    await app.mount_app("/actions")
    await _open_history_receipts(view.page)
    page = view.page
    await expect(page.get_by_text("Delete Pod apps/web-0", exact=True)).to_be_visible()
    await expect(page.get_by_text("Tool error", exact=True).first).to_be_visible()
    await expect(page.get_by_text("Deletion accepted", exact=True)).to_have_count(0)
    await expect(page.get_by_text("Result", exact=True)).to_be_visible()
    await expect(page.get_by_text("Pod deleted successfully", exact=True)).to_be_visible()
    await view.check(context="error envelope is retained instead of a success receipt")
    await view.capture(target=page.locator("#app"))


if __name__ == "__main__":
    pytest_bazel.main()
