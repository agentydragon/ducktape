"""Shared assertions for the production Action cards at desktop and mobile widths."""

import json
from dataclasses import dataclass

import pytest
from playwright.async_api import Locator, expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from testing.frontend_visual.capture import VisualPage
from testing.frontend_visual.page_capture import wait_for_stable
from testing.frontend_visual.viewports import MOBILE, Viewport


@dataclass(frozen=True)
class ActionExample:
    group: str
    name: str
    arguments: dict[str, object]
    result: object
    result_text: str
    # False where the closed compact card already shows every argument, so it has no expanded version.
    compact_card_expands: bool = True
    live_preview: tuple[str, dict[str, object], str] | None = None
    mcp_text: str | None = None
    wire_result: dict[str, object] | None = None

    @property
    def key(self) -> str:
        return f"{self.group}.{self.name}"


CALENDAR_ID = "facilities@example.test"

CALENDAR_NAME = "Facilities calendar"

CALENDAR_RECORDED_NAME = "Facilities schedule"

CALENDAR_URL = "https://calendar.google.com/calendar?cid=ZmFjaWxpdGllc0BleGFtcGxlLnRlc3Q"

CALENDAR_LOCAL_RANGE = "Mon, Nov 23, 2026 · 5:00 PM \u2013 5:30 PM"

EXAMPLE_ENVIRONMENT = {"name": "review-box", "namespace": "agentplane", "uid": "environment-1"}

EXAMPLE_SESSION = "81000000-0000-4000-8000-000000000001"

EXAMPLE_COMMAND = "81000000-0000-4000-8000-000000000002"

COMPACT_SCOPED_READS = {
    "tana.read_node": ("Plan review", "Include descendants"),
    "tana.get_children": ("Direct children of Plan review",),
    "tana.list_workspaces": ("List loaded Tana workspaces",),
    "tana.list_tags": ("Product workspace and linked workspaces",),
    "sessions.get": (EXAMPLE_SESSION,),
    "sessions.read_turns": (EXAMPLE_SESSION, "Up to 5 turns from History · next page"),
    "orchestration_v1.get_session": ("Sandbox review-box", EXAMPLE_SESSION),
    "orchestration_v1.list_sessions": ("Sandbox review-box",),
    "orchestration_v1.read_turns": ("Sandbox review-box", EXAMPLE_SESSION, "Up to 5 recent turns"),
}

TANA_READ_LIMITS = {
    "tana.read_node": "Up to 2 levels of descendants",
    "tana.get_children": "Up to 1 direct child, after the first 1",
    "tana.list_tags": "Up to 25 tags",
}

GMAIL_LABEL = {
    "id": "Label_19",
    "name": "Deployments/Reviewed",
    "type": "user",
    "threadsTotal": 3,
    "threadsUnread": 0,
    "labelListVisibility": "labelShowIfUnread",
    "messageListVisibility": "show",
}


async def _expect_live_preview(surface: Locator, example: ActionExample) -> None:
    if example.live_preview is None:
        return
    _, payload, label = example.live_preview
    if "workspace" in payload and "nodes" not in payload:
        await expect(surface.get_by_text(label).first).to_be_visible()
    else:
        link = surface.get_by_role("link", name=label, exact=True).first
        await expect(link).to_be_visible()
        if example.group == "google_calendar":
            await expect(link).to_have_attribute("href", CALENDAR_URL)
            await expect(surface.get_by_text(CALENDAR_ID, exact=True)).to_have_count(0)


class ActionPresentationCases:
    examples: tuple[ActionExample, ...]

    @classmethod
    def pytest_generate_tests(cls, metafunc: pytest.Metafunc) -> None:
        metafunc.parametrize("example", cls.examples, ids=lambda example: example.key)

    @DESKTOP_AND_MOBILE
    async def test_action_presentations(
        self, view: VisualPage, app: AgentplaneFixture, example: ActionExample, viewport: Viewport
    ) -> None:
        form_factor = "mobile" if viewport == MOBILE else "desktop"

        async def capture(checkpoint: str, target: Locator) -> None:
            await view.capture(checkpoint, target=target, label=f"{example.key} · {form_factor} · {checkpoint}")

        page = view.page
        registered = await page.evaluate("window.agentplaneVisual.actionRendererKeys()")
        keys = [item.key for item in self.examples]
        assert len(keys) == len(set(keys)), "Duplicate Action visual example"
        assert set(keys) == set(registered), "Every registered Action renderer needs a named visual example"

        summaries: dict[str, object] | None = (
            {example.live_preview[0]: example.live_preview[1]} if example.live_preview else None
        )
        if example.group == "grocy_sf" and summaries is None:
            summaries = {"grocy-reference-summaries": {"references": []}}
        if example.key == "github.push_files":
            summaries = {"github-push-files-preview": {}}
        gmail_labeled_read = example.key in {"gmail.messages_get", "gmail.threads_get"}
        if gmail_labeled_read:
            summaries = {
                "gmail-resource-summaries": {"labels": [{"id": GMAIL_LABEL["id"], "title": GMAIL_LABEL["name"]}]}
            }
        # Native results are stored directly. MCP results use the actual wire envelope.
        mcp = example.group not in {"environments", "orchestration_v1", "sandbox", "sessions"}
        result = (
            example.result
            if not mcp
            else example.wire_result
            if example.wire_result is not None
            else {"content": [], "structuredContent": {"result": None}, "_meta": {"fastmcp": {"wrap_result": True}}}
            if example.result is None
            else {
                "content": [
                    {
                        "type": "text",
                        "text": example.mcp_text
                        if example.mcp_text is not None
                        else example.result
                        if isinstance(example.result, str)
                        else json.dumps(example.result),
                    }
                ],
                **({"structuredContent": example.result} if isinstance(example.result, dict) else {}),
                "isError": False,
            }
        )
        await app.show_action_receipt(example.group, example.name, example.arguments, result)
        await app.show_action_preview(example.group, example.name, example.arguments, summaries)
        # The shell uses a 240px desktop sidebar and a full-width phone drawer. The queue
        # leaves 8px per side, and routed cards have two 16px content gutters.
        sidebar_width = (viewport.width if viewport == MOBILE else 240) - 16
        detail_width = viewport.width - (0 if viewport == MOBILE else 240) - 32
        await app.mount_action_presentations(sidebar_width=sidebar_width, detail_width=detail_width, mcp=mcp)
        sidebar = page.locator("[data-action-visual=sidebar]")
        await sidebar.scroll_into_view_if_needed()
        await expect(sidebar.locator(".agentplane-actions-sidebar-name")).to_be_visible()
        await expect(sidebar.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
        if example.key == "google_calendar.delete_event":
            await expect(sidebar).to_contain_text("Facilities coordination")
            await expect(sidebar).to_contain_text("Only this occurrence")
        elif example.key == "google_calendar.update_event":
            await expect(sidebar).to_contain_text("Facilities calendar")
            await expect(sidebar).to_contain_text("Changes: title, location, attendees, reminders")
            await expect(sidebar).not_to_contain_text("Entire recurring series")
            await expect(sidebar).not_to_contain_text("Remove all attendees")
        elif example.key == "google_calendar.list_events":
            await expect(sidebar).to_contain_text(CALENDAR_NAME)
            await expect(sidebar).to_contain_text(CALENDAR_ID)
            await expect(sidebar).not_to_contain_text("Ending after")
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
        elif example.key == "grocy_sf.shopping_list_item_edit":
            await expect(sidebar).to_contain_text("Kefir")
            await expect(sidebar).to_contain_text("Mark done · Clear note")
        elif example.key == "grocy_sf.shopping_list_items_add":
            await expect(sidebar).to_contain_text("2 stock units of Kefir for Weekly groceries")
            await expect(sidebar).to_contain_text("Note · Plain")
        elif example.key in {"tana.check_node", "tana.uncheck_node"}:
            await expect(sidebar).to_contain_text("Plan review")
            await expect(sidebar).to_contain_text("Syncs the mapped field if configured")
        elif example.key == "tana.tag":
            await expect(sidebar).to_contain_text("Add Task to Plan review")
        elif example.key == "tana.move_node":
            await expect(sidebar).to_contain_text("Draft notes")
            await expect(sidebar).to_contain_text("Website project")
        elif example.key == "tana.get_tag_schema":
            tag = sidebar.get_by_role("link", name="Task", exact=True)
            await expect(tag).to_have_attribute("href", "https://app.tana.inc?nodeid=tag-1")
            await expect(sidebar).not_to_contain_text("Include inherited fields")
            await expect(sidebar).not_to_contain_text("Include edit instructions")
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
            await expect(sidebar.get_by_role("button", name="Deny ", exact=False)).to_be_visible()
        elif example.key == "kubernetes_admin.pods_run":
            await expect(sidebar).to_contain_text("Expose TCP port 8080 through a ClusterIP Service.")
            await expect(sidebar).not_to_contain_text("OpenShift Route")
        elif example.key == "orchestration_v1.launch_sandbox":
            await expect(sidebar).to_contain_text("review-box")
            await expect(sidebar).to_contain_text("docs-read")
            await expect(sidebar).to_contain_text("Send inbox messages")
        elif example.key == "gmail.threads_modify_labels":
            thread = sidebar.get_by_role("link", name="Deployment review", exact=True)
            await expect(thread).to_have_attribute("href", "https://mail.google.com/mail/u/0/#all/thread-123")
            await expect(sidebar).to_contain_text("Reviewed")
            await expect(sidebar).to_contain_text("Needs review")
            await expect(sidebar).not_to_contain_text("Creates missing labels")
        elif example.key == "github.issue_write":
            issue = sidebar.get_by_role("link", name="example/service#7", exact=True)
            await expect(issue).to_have_attribute("href", "https://github.com/example/service/issues/7")
            await expect(sidebar).to_contain_text("Edit issue fields Priority")
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_have_count(0)
        if example.key in {"gmail.threads_modify_labels", "grocy_sf.products_edit"}:
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_have_count(0)
        if example.key in {
            "grocy_sf.locations_create",
            "grocy_sf.product_groups_create",
            "grocy_sf.quantity_units_create",
            "grocy_sf.shopping_lists_create",
            "grocy_sf.shopping_list_item_edit",
            "grocy_sf.shopping_list_items_add",
            "kubernetes_admin.pods_run",
            "kubernetes_admin.resources_scale",
            "tana.check_node",
            "tana.get_or_create_calendar_node",
            "tana.tag",
            "tana.uncheck_node",
        }:
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
        if example.key in COMPACT_SCOPED_READS:
            for field in COMPACT_SCOPED_READS[example.key]:
                await expect(sidebar).to_contain_text(field)
            await _expect_live_preview(sidebar, example)
            if example.arguments.get("session_id") == EXAMPLE_SESSION:
                session = sidebar.get_by_role("link", name=EXAMPLE_SESSION, exact=True)
                await expect(session).to_have_attribute("href", f"/threads/{EXAMPLE_SESSION}")
            await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
            await expect(sidebar.get_by_role("button", name="Deny ", exact=False)).to_be_visible()
        read_limit = TANA_READ_LIMITS.get(example.key)
        if read_limit is not None:
            await expect(sidebar).not_to_contain_text(read_limit)
        await capture("sidebar-collapsed", sidebar)
        disclosure = sidebar.get_by_role("button", name="Expand ", exact=False)
        if example.compact_card_expands:
            if example.key == "gmail.threads_modify_labels":
                thread = sidebar.get_by_role("link", name="Deployment review", exact=True)
                gmail_url = "https://mail.google.com/mail/u/0/#all/thread-123"
                await page.context.route(
                    "https://mail.google.com/**", lambda route: route.fulfill(body="Gmail fixture")
                )
                await thread.focus()
                await expect(thread).to_be_focused()
                async with page.expect_popup() as popup_info:
                    if viewport == MOBILE:
                        await thread.click()
                    else:
                        await thread.press("Enter")
                popup = await popup_info.value
                await expect(popup).to_have_url(gmail_url)
                await popup.close()
                await expect(disclosure).to_have_attribute("aria-expanded", "false")
                await disclosure.press("Enter")
                await expect(sidebar.get_by_role("button", name="Collapse ", exact=False)).to_have_attribute(
                    "aria-expanded", "true"
                )
                await sidebar.get_by_role("button", name="Collapse ", exact=False).press("Enter")
            await sidebar.locator(".agentplane-actions-sidebar-summary").click(position={"x": 25, "y": 8})
            await _expect_live_preview(sidebar, example)
            if example.key == "google_calendar.update_event":
                when = sidebar.get_by_role("button", name=CALENDAR_LOCAL_RANGE, exact=True)
                await when.hover()
                tooltip = page.get_by_role("tooltip")
                await expect(tooltip).to_contain_text("2026-11-23T09:00:00-08:00 · America/Los_Angeles")
                await page.mouse.move(0, 0)
                await expect(tooltip).to_have_count(0)
                await sidebar.get_by_role("link", name="Facilities coordination", exact=True).focus()
                await page.keyboard.press("Tab")
                await expect(when).to_be_focused()
                await expect(tooltip).to_contain_text("2026-11-23T09:30:00-08:00 · America/Los_Angeles")
                await when.click()
                await expect(tooltip).to_be_visible()
                await sidebar.locator(".agentplane-actions-sidebar-expand").focus()
            if example.key == "google_calendar.update_event":
                await expect(sidebar).to_contain_text("Entire recurring series")
                await expect(sidebar).to_contain_text("Remove all attendees")
            if example.key in {"gmail.threads_modify_labels", "grocy_sf.products_edit"}:
                await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
            elif read_limit is not None:
                await expect(sidebar).to_contain_text(read_limit)
                await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
            elif example.key == "google_calendar.list_events":
                await expect(sidebar).to_contain_text("Ending after")
                await expect(sidebar).to_contain_text("Starting before")
            elif example.key == "github.push_files":
                await expect(
                    sidebar.get_by_text("Comparison unavailable. Files show proposed contents.")
                ).to_be_visible()
            elif example.key == "tana.get_tag_schema":
                await expect(sidebar).to_contain_text("Include inherited fields")
                await expect(sidebar).to_contain_text("Include edit instructions")
                await expect(sidebar.get_by_role("button", name="Approve ", exact=False)).to_be_visible()
            elif example.key == "github.issue_write":
                await expect(sidebar).to_contain_text("Priority P1")
                await expect(sidebar).to_contain_text("Decisions require full review.")
            await expect(sidebar.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
            await wait_for_stable(page)
            await expect(sidebar.locator(".ui-code-block-placeholder")).to_have_count(0)
            await capture("sidebar-expanded", sidebar)
            await expect(sidebar.get_by_role("link", name="Details", exact=True)).to_be_visible()
        else:
            await expect(disclosure).to_have_count(0)
            await expect(sidebar.get_by_role("link", name="View details for ", exact=False)).to_be_visible()

        pending = page.locator("[data-action-visual=approval]")
        await pending.scroll_into_view_if_needed()
        await expect(pending.get_by_role("button", name="Approve", exact=True)).to_be_visible()
        await expect(pending).to_contain_text("Check the exact call before approving.")
        await _expect_live_preview(pending, example)
        await expect(pending.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
        if example.key == "gmail.threads_modify_labels":
            subject_link = pending.get_by_role("link", name="Deployment review", exact=True)
            await expect(subject_link).to_have_attribute("href", "https://mail.google.com/mail/u/0/#all/thread-123")
            await expect(subject_link).to_have_attribute("target", "_blank")
        elif example.key == "grocy_sf.products_edit":
            await expect(pending).to_contain_text("Cupboard → Pantry")
            await expect(pending).to_contain_text("Keep dry → Clear")
        elif example.key == "grocy_sf.shopping_list_item_edit":
            await expect(pending).to_contain_text("Not done → Done")
        elif example.key == "grocy_sf.shopping_list_items_remove":
            await expect(pending).to_contain_text("Weekly groceries · 2 Bottle")
        elif example.key == "tana.get_tag_schema":
            await expect(pending).to_contain_text("Include inherited fields")
            await expect(pending).to_contain_text("Include edit instructions")
        elif example.key == "tana.move_node":
            await expect(pending).to_contain_text(
                "Move Draft notes to the start of Website project, keeping the source reference."
            )
            for name, node_id in (("Draft notes", "draft"), ("Website project", "website")):
                node_link = pending.get_by_role("link", name=name, exact=True)
                await expect(node_link).to_have_attribute("href", f"https://app.tana.inc?nodeid={node_id}")
                await expect(node_link).to_have_attribute("target", "_blank")
                await expect(pending.get_by_text(node_id, exact=True)).to_have_count(0)
        if example.key == "grocy_sf.products_merge":
            await expect(pending.get_by_role("link", name="Ground coffee", exact=True)).to_have_attribute(
                "href", "https://grocy-sf.allegedly.works/product/16"
            )
            await expect(pending.get_by_role("link", name="Product #15", exact=True)).to_be_visible()
        if example.key == "google_calendar.delete_event":
            await expect(pending).to_contain_text("Only this occurrence")
            event_link = pending.get_by_role("link", name="Facilities coordination", exact=True)
            await expect(event_link).to_have_attribute(
                "href", "https://calendar.google.com/calendar/event?eid=facilities-series_20261130T170000Z"
            )
        elif example.key == "google_calendar.update_event":
            await expect(pending).to_contain_text("Entire recurring series")
            await expect(pending).to_contain_text("Remove all attendees")

        await pending.evaluate("element => element.scrollIntoView({block: 'end'})")
        await expect(pending.locator(".ui-code-block-placeholder")).to_have_count(0)
        await capture("pending-detail", pending)
        if example.key == "home_assistant.ha_config_set_automation":
            await expect(pending.get_by_text("Study Lighting", exact=False)).to_be_visible()
            config = pending.get_by_role("button", name="Show configuration", exact=True)
            await expect(config).to_have_attribute("aria-expanded", "false")
            await config.click()
            await expect(pending.locator(".cm-editor").first).to_be_visible()

        receipt = page.locator("[data-action-visual=receipt]")
        await expect(receipt).to_be_visible()
        await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
        await expect(receipt).to_contain_text(example.result_text)
        if gmail_labeled_read:
            await expect(receipt).to_contain_text("Inbox, Deployments/Reviewed, Label_99")
            await expect(receipt).not_to_contain_text("Label_19")
        if example.key == "grocy_sf.products_edit":
            await expect(receipt.get_by_role("link", name="Rolled oats", exact=True)).to_have_attribute(
                "href", "https://grocy-sf.allegedly.works/product/2"
            )
            await expect(receipt.get_by_text("Product #2", exact=True)).to_have_count(0)
        elif example.key == "grocy_sf.shopping_list_item_edit":
            await expect(receipt.get_by_role("link", name="Kefir", exact=True)).to_have_attribute(
                "href", "https://grocy-sf.allegedly.works/shoppinglistitem/52"
            )
            await expect(receipt.get_by_text("Shopping item #52", exact=True)).to_have_count(0)
        elif example.key == "grocy_sf.shopping_list_items_remove":
            await expect(receipt.get_by_text("Kefir · 2 Bottle", exact=True)).to_be_visible()
            await expect(receipt.get_by_text("Shopping item #52", exact=True)).to_have_count(0)
        if example.key == "home_assistant.ha_get_state":
            await expect(receipt.get_by_role("link", name="Greenhouse temperature", exact=True)).to_be_visible()
            await expect(receipt.get_by_text("sensor.greenhouse_temperature", exact=True)).to_have_count(0)
        if example.key == "home_assistant.ha_get_entity":
            await expect(receipt.get_by_role("link", name="Greenhouse fan", exact=True)).to_be_visible()
            await expect(receipt.get_by_text("fan.greenhouse", exact=True)).to_have_count(0)
        if example.group not in {"google_calendar", "grocy_sf"}:
            await _expect_live_preview(receipt, example)
        if example.key == "google_calendar.list_events":
            calendar_link = receipt.get_by_role("link", name=CALENDAR_RECORDED_NAME, exact=True)
            await expect(calendar_link).to_have_attribute("href", CALENDAR_URL)
            await expect(calendar_link).to_have_count(1)
            await expect(receipt.get_by_text(CALENDAR_ID, exact=True)).to_have_count(0)
        if example.key in {"google_calendar.get_event", "google_calendar.update_event"}:
            await expect(receipt.get_by_text(str(example.arguments["event_id"]), exact=True)).to_have_count(0)
        if example.key == "tana.get_or_create_calendar_node":
            node_link = receipt.get_by_role("link", name=example.result_text)
            await expect(node_link).to_have_attribute("href", "https://app.tana.inc?nodeid=calendar-week-42")
            await expect(receipt.get_by_text("calendar-week-42", exact=True)).to_have_count(0)
        elif example.key == "tana.move_node":
            result = receipt.get_by_test_id("action-execution-outcome")
            node_link = result.get_by_role("link", name="Planning inbox")
            await expect(node_link).to_have_attribute("href", "https://app.tana.inc?nodeid=inbox")
            await expect(result.get_by_text("website", exact=True)).to_have_count(0)

        await expect(receipt.locator(".ui-code-block-placeholder")).to_have_count(0)
        await expect(receipt.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
        await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
        await capture("detail-result", receipt)

        history = page.locator("[data-action-visual=history]")
        details = history.get_by_role("button", name="Receipt details", exact=True)
        await expect(details).to_have_attribute("aria-expanded", "false")
        await history.evaluate("element => element.scrollIntoView({block: 'end'})")
        await capture("history", history)

        await details.click()
        await expect(details).to_have_attribute("aria-expanded", "true")
        await history.evaluate("element => element.scrollIntoView({block: 'end'})")
        await expect(history).to_contain_text(example.result_text)
        if gmail_labeled_read:
            await expect(history).to_contain_text("Inbox, Deployments/Reviewed, Label_99")
            await expect(history).not_to_contain_text("Label_19")
        if example.group not in {"google_calendar", "grocy_sf"}:
            await _expect_live_preview(history, example)
        if example.key == "google_calendar.list_events":
            calendar_link = history.get_by_role("link", name=CALENDAR_RECORDED_NAME, exact=True)
            await expect(calendar_link).to_have_attribute("href", CALENDAR_URL)
            await expect(calendar_link).to_have_count(1)
            await expect(history.get_by_text(CALENDAR_ID, exact=True)).to_have_count(0)
        if example.key in {"google_calendar.get_event", "google_calendar.update_event"}:
            await expect(history.get_by_text(str(example.arguments["event_id"]), exact=True)).to_have_count(0)
        await expect(history.locator(".ui-code-block-placeholder")).to_have_count(0)
        await capture("history-expanded", history)

        if gmail_labeled_read:
            raw_switch = receipt.get_by_role("switch", name="Raw", exact=True)
            await raw_switch.press("Space")
            await expect(raw_switch).to_be_checked()
            raw_result = receipt.get_by_test_id("action-execution-outcome")
            if example.name == "threads_get":
                await raw_result.get_by_role("button", name="Show more", exact=True).click()
            await expect(raw_result).to_contain_text('"Label_19"')
            await expect(raw_result).to_contain_text('"Label_99"')

        if example.key in {"google_calendar.get_event", "google_calendar.update_event", "google_calendar.list_events"}:
            raw_key = "calendar_id" if example.name == "list_events" else "event_id"
            for surface in (receipt, history):
                raw_switch = surface.get_by_role("switch", name="Raw", exact=True)
                await raw_switch.press("Space")
                await expect(raw_switch).to_be_checked()
                await expect(surface).to_contain_text(f'"{raw_key}": "{example.arguments[raw_key]}"')

        if example.key == "google_calendar.delete_event":
            await expect(receipt.get_by_role("link", name="Facilities coordination", exact=True)).to_have_count(0)
            raw_switch = receipt.get_by_role("switch", name="Raw", exact=True)
            await raw_switch.press("Space")
            await expect(raw_switch).to_be_checked()
            await expect(receipt).to_contain_text('"event_id": "facilities-series_20261130T170000Z"')
            await expect(receipt).to_contain_text(f'"calendar_id": "{CALENDAR_ID}"')
            await expect(receipt).to_contain_text('"content": []')
            await expect(receipt).to_contain_text('"result": null')
        elif example.key == "grocy_sf.shopping_list_items_remove":
            raw_switch = receipt.get_by_role("switch", name="Raw", exact=True)
            await raw_switch.press("Space")
            await expect(raw_switch).to_be_checked()
            await expect(receipt).to_contain_text('"item_ids": [')
            await expect(receipt).to_contain_text(r"\"item_id\": 52")
