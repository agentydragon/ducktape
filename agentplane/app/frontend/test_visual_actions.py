"""One named set of real-host screenshots per registered Action renderer."""

import json
from dataclasses import dataclass

import pytest
import pytest_bazel
from playwright.async_api import expect

from agentplane.app.frontend.testing.app import IDLE_THREAD, AgentplaneFixture
from agentplane.app.frontend.testing.visual_assertions import _open_phone_navigation
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from util.testing.page_capture import wait_for_stable
from util.testing.visual_capture import VisualPage

pytest_plugins = ("util.testing.visual_fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass(frozen=True)
class ActionExample:
    group: str
    name: str
    arguments: dict[str, object]
    result: object
    result_text: str
    # False where the closed compact card already shows every argument, so it has no expanded version.
    compact_card_expands: bool = True

    @property
    def key(self) -> str:
        return f"{self.group}.{self.name}"


CONFIG_MAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "settings", "namespace": "apps"},
    "data": {"mode": "safe"},
}
POD = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "web-0", "namespace": "apps"}}
SANDBOX = {"name": "review-sandbox", "template": "python", "phase": "Running", "conditions": []}

# Authored examples, not defaults generated from schemas: show meaningful optional arguments and
# domain results. Names are also the gallery identities. The test checks these against the registry.
EXAMPLES = (
    ActionExample(
        "kubernetes_admin",
        "pods_list_in_namespace",
        {"namespace": "apps", "labelSelector": "app=web", "fieldSelector": "status.phase=Running"},
        [POD],
        "web-0",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin", "resources_create_or_update", {"resource": json.dumps(CONFIG_MAP)}, CONFIG_MAP, "settings"
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_delete",
        {"name": "web-0", "namespace": "apps"},
        "Pod deleted successfully",
        "Deletion accepted",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_exec",
        {"name": "web-0", "namespace": "apps", "container": "main", "command": ["printf", "%s", "hello world"]},
        "hello world",
        "hello world",
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_get",
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "settings", "namespace": "apps"},
        CONFIG_MAP,
        "safe",
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_list",
        {"apiVersion": "v1", "kind": "Pod", "namespace": "apps", "labelSelector": "app=web"},
        [POD],
        "web-0",
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_delete",
        {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps", "gracePeriodSeconds": 0},
        "Resource deleted successfully",
        "Deletion accepted",
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_get",
        {"name": "web-0", "namespace": "apps"},
        POD,
        "apiVersion",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_log",
        {"name": "web-0", "namespace": "apps", "container": "main", "previous": True, "tail": 100},
        "Starting web server\nListening on :8080\n",
        "Listening on :8080",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "events_list",
        {"namespace": "apps", "fieldSelector": "type=Warning"},
        [{"reason": "BackOff", "message": "Back-off restarting failed container"}],
        "BackOff",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "push_files",
        {
            "owner": "example",
            "repo": "service",
            "branch": "docs",
            "message": "docs: document deployment checks",
            "files": [
                {
                    "path": "docs/deployment.md",
                    "content": "# Deployment checks\n\nWait for readiness before shifting traffic.\n",
                },
                {"path": "deploy/checks.py", "content": "def ready(status: str) -> bool:\n    return status == 'ok'\n"},
            ],
        },
        {"ref": "refs/heads/docs", "object": {"sha": "0123456789abcdef0123456789abcdef01234567", "type": "commit"}},
        "0123456789abcdef",
    ),
    ActionExample(
        "github",
        "delete_file",
        {
            "owner": "example",
            "repo": "service",
            "path": "docs/old-guide.md",
            "message": "docs: remove old deployment guide",
            "branch": "docs",
        },
        {"commit": {"sha": "0123456789abcdef0123456789abcdef01234567"}, "content": None},
        "0123456789abcdef",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "update_pull_request",
        {
            "owner": "example",
            "repo": "service",
            "pullNumber": 42,
            "state": "closed",
            "body": "Closed in favor of a smaller change.\nThe readiness checks move to a follow-up.",
        },
        {"id": "4200", "html_url": "https://github.com/example/service/pull/42"},
        "pull/42",
    ),
    ActionExample(
        "github",
        "update_pull_request_branch",
        {"owner": "example", "repo": "service", "pullNumber": 42, "expectedHeadSha": "0123456789abcdef"},
        {"status": "updated"},
        "updated",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "create_or_update_file",
        {
            "owner": "example",
            "repo": "service",
            "path": "docs/deployment.md",
            "content": "# Deployment checks\n\nWait for readiness before shifting traffic.\n",
            "message": "docs: document deployment checks",
            "branch": "docs",
            "allow_symlink_write": False,
        },
        {"commit": {"sha": "0123456789abcdef0123456789abcdef01234567"}},
        "0123456789abcdef",
    ),
    ActionExample(
        "github",
        "create_branch",
        {"owner": "example", "repo": "service", "branch": "docs", "from_branch": "devel"},
        {"ref": "refs/heads/docs", "object": {"sha": "0123456789abcdef0123456789abcdef01234567", "type": "commit"}},
        "refs/heads/docs",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "create_pull_request",
        {
            "owner": "example",
            "repo": "service",
            "title": "Document deployment checks",
            "head": "docs",
            "base": "devel",
            "body": "Explain readiness checks.\nInclude rollback instructions.",
            "draft": True,
            "maintainer_can_modify": False,
            "reviewers": ["reviewer"],
        },
        {"number": 42, "html_url": "https://github.com/example/service/pull/42"},
        "42",
    ),
    ActionExample(
        "github",
        "search_pull_requests",
        {
            "owner": "example",
            "repo": "service",
            "query": "is:open label:bug",
            "page": 2,
            "perPage": 10,
            "sort": "updated",
            "order": "desc",
            "fields": ["number", "title", "state", "html_url"],
        },
        {
            "total_count": 1,
            "items": [
                {
                    "number": 42,
                    "title": "Handle deployment checks",
                    "state": "open",
                    "html_url": "https://github.com/example/service/pull/42",
                }
            ],
        },
        "Handle deployment checks",
        compact_card_expands=False,
    ),
    ActionExample(
        "gmail",
        "drafts_create",
        {
            "to": ["owner@example.com"],
            "cc": ["reviewer@example.com"],
            "bcc": ["archive@example.com"],
            "subject": "Deployment review",
            "body": "Please review the deployment.\nReply with any blockers.",
            "thread_id": "thread-123",
        },
        {"id": "draft-123", "message": {"id": "message-123", "threadId": "thread-123"}},
        "draft-123",
    ),
    ActionExample(
        "gmail",
        "threads_list",
        {"q": "from:deployments@example.com", "maxResults": 5, "includeSpamTrash": True},
        {"threads": [{"id": "thread-123", "snippet": "Deployment review requested"}], "resultSizeEstimate": 1},
        "Deployment review requested",
    ),
    ActionExample(
        "grocy_sf",
        "products_list",
        {"detail": "full"},
        [{"id": 1, "name": "Oat milk"}, {"id": 2, "name": "Coffee"}],
        "Oat milk",
    ),
    ActionExample(
        "grocy_sf",
        "quantity_units_list",
        {"detail": "full"},
        [{"id": 1, "name": "Bottle"}, {"id": 2, "name": "Gram"}],
        "Bottle",
    ),
    ActionExample(
        "grocy_sf",
        "get_system_info",
        {},
        {"grocy_version": {"Version": "4.5.0"}, "php_version": "8.4", "sqlite_version": "3.50", "db_version": 1},
        "4.5.0",
        compact_card_expands=False,
    ),
    ActionExample(
        "tana",
        "get_or_create_calendar_node",
        {"workspaceId": "workspace-123", "granularity": "week", "date": "2026-10-12"},
        {"nodeId": "calendar-week-42"},
        "calendar-week-42",
        compact_card_expands=False,
    ),
    ActionExample(
        "sandbox",
        "exec",
        {
            "name": "review-sandbox",
            "script": "printf '%s\\n' 'ready for review'",
            "timeout_seconds": 60,
            "max_output_bytes": 4096,
            "cwd": "/workspace",
        },
        {
            "exit": {"kind": "exited", "exit_code": 0},
            "stdout": "ready for review\n",
            "stderr": "",
            "duration_seconds": 0.1,
        },
        "ready for review",
    ),
    ActionExample(
        "sandbox",
        "create",
        {"name": "review-sandbox", "template": "python"},
        SANDBOX,
        "Running",
        compact_card_expands=False,
    ),
    ActionExample("sandbox", "get", {"name": "review-sandbox"}, SANDBOX, "Running", compact_card_expands=False),
    ActionExample(
        "sandbox",
        "dispose",
        {"name": "review-sandbox"},
        {"name": "review-sandbox", "existed": True},
        "disposed",
        compact_card_expands=False,
    ),
    ActionExample("sandbox", "list", {}, {"sandboxes": [SANDBOX]}, "Running", compact_card_expands=False),
    ActionExample(
        "ssh",
        "exec",
        {"host": "archive.example.com", "user": "backup", "command": "ls -lh /srv/backups", "timeout_seconds": 30},
        {
            "host": "archive.example.com",
            "user": "backup",
            "exit_code": 0,
            "stdout": "-rw-r--r-- 1 backup backup 1.5M Oct 12 03:00 daily.tar.zst\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        },
        "daily.tar.zst",
    ),
)


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.key)
async def test_action_presentations(
    view: VisualPage, app: AgentplaneFixture, example: ActionExample, capture_name: str
) -> None:
    page = view.page
    registered = await page.evaluate("window.agentplaneVisual.actionRendererKeys()")
    keys = [item.key for item in EXAMPLES]
    assert len(keys) == len(set(keys)), "Duplicate Action visual example"
    assert set(keys) == set(registered), "Every registered Action renderer needs a named visual example"

    await app.show_action_preview(example.group, example.name, example.arguments)
    await app.mount_thread(IDLE_THREAD)
    await _open_phone_navigation(page)
    sidebar = page.locator(".agentplane-actions-sidebar")
    await expect(sidebar.locator(".agentplane-actions-sidebar-name")).to_be_visible()
    await expect(sidebar.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
    await wait_for_stable(page)
    await view.capture(f"{capture_name}-sidebar-collapsed", target=sidebar)
    disclosure = sidebar.get_by_role("button", name="Expand ", exact=False)
    if example.compact_card_expands:
        await disclosure.click()
        await expect(sidebar.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
        await wait_for_stable(page)
        await expect(sidebar.locator(".ui-code-block-placeholder")).to_have_count(0)
        await wait_for_stable(page)
        await view.capture(f"{capture_name}-sidebar-expanded", target=sidebar)
        review_link = sidebar.get_by_role("link", name="Details", exact=True)
    else:
        await expect(disclosure).to_have_count(0)
        review_link = sidebar.get_by_role("link", name="View details for ", exact=False)

    # Follow the actual review link so pending detail controls and argument composition are visible too.
    await review_link.click()
    pending = page.locator(".agentplane-shell-main .mantine-Paper-root").filter(
        has=page.get_by_role("button", name="Approve", exact=True)
    )
    await expect(pending).to_have_count(1)
    await expect(pending).to_contain_text("Check the exact call before approving.")
    await expect(pending.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
    await pending.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(pending.locator(".ui-code-block-placeholder")).to_have_count(0)
    await wait_for_stable(page)
    await view.capture(f"{capture_name}-pending-detail", target=pending)

    # Native sandbox results are stored directly. MCP results use the actual wire envelope.
    result = (
        example.result
        if example.group == "sandbox"
        else {
            "content": [
                {
                    "type": "text",
                    "text": example.result if isinstance(example.result, str) else json.dumps(example.result),
                }
            ],
            **({"structuredContent": example.result} if isinstance(example.result, dict) else {}),
            "isError": False,
        }
    )
    await app.show_action_receipt(example.group, example.name, example.arguments, result)
    await page.evaluate("window.location.hash = '/actions/70000000-0000-4000-8000-000000000007'")
    receipt = page.locator("[data-action-receipt]")
    await expect(receipt).to_be_visible()
    await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(receipt).to_contain_text(example.result_text)
    await expect(receipt.locator(".ui-code-block-placeholder")).to_have_count(0)
    await expect(receipt.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
    await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
    await wait_for_stable(page)
    await view.capture(f"{capture_name}-detail-result", target=receipt)

    await page.evaluate("window.location.hash = '/actions'")
    await expect(page.get_by_role("radio", name="Not auto-allowed", exact=True)).to_be_checked()
    await expect(page.locator("[data-action-receipt]")).to_have_count(1)
    history = page.locator("[data-action-receipt]")
    details = history.get_by_role("button", name="Receipt details", exact=True)
    await expect(details).to_have_attribute("aria-expanded", "false")
    await history.evaluate("element => element.scrollIntoView({block: 'end'})")
    await wait_for_stable(page)
    await view.capture(f"{capture_name}-history", target=history)

    await details.click()
    await expect(details).to_have_attribute("aria-expanded", "true")
    await history.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(history).to_contain_text(example.result_text)
    await expect(history.locator(".ui-code-block-placeholder")).to_have_count(0)
    await wait_for_stable(page)
    await view.capture(f"{capture_name}-history-expanded", target=history)


if __name__ == "__main__":
    pytest_bazel.main()
