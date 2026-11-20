"""One named set of real-host screenshots per registered Action renderer."""

import json
from dataclasses import dataclass

import pytest
import pytest_bazel
from playwright.async_api import Locator, expect

from agentplane.app.frontend.testing.app import AgentplaneFixture
from agentplane.app.frontend.testing.visual_fixtures import DESKTOP_AND_MOBILE
from testing.frontend_visual.capture import VisualPage
from testing.frontend_visual.page_capture import wait_for_stable
from testing.frontend_visual.viewports import MOBILE, Viewport

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
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
    live_preview: tuple[str, dict[str, object], str] | None = None

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

CALENDAR_ID = "facilities@example.test"
CALENDAR_START = {"date_time": "2026-11-23T09:00:00-08:00", "time_zone": "America/Los_Angeles"}
CALENDAR_END = {"date_time": "2026-11-23T09:30:00-08:00", "time_zone": "America/Los_Angeles"}
CALENDAR_SERIES = {
    "event_id": "facilities-series",
    "summary": "Facilities coordination",
    "html_link": "https://calendar.google.com/calendar/event?eid=facilities-series",
    "status": "confirmed",
    "start": CALENDAR_START,
    "end": CALENDAR_END,
    "location": "Meeting room 2",
    "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
    "attendees": [
        {"email": "coordinator@example.test", "display_name": "Facilities coordinator", "response_status": "accepted"}
    ],
}
CALENDAR_ALL_DAY = {
    "event_id": "office-closure",
    "summary": "Office closure",
    "html_link": "https://calendar.google.com/calendar/event?eid=office-closure",
    "start": {"date": "2026-11-26"},
    "end": {"date": "2026-11-28"},
}
CALENDAR_OCCURRENCE = {
    "event_id": "facilities-series_20261130T170000Z",
    "status": "cancelled",
    "recurring_event_id": "facilities-series",
    "original_start_time": {"date_time": "2026-11-30T09:00:00-08:00", "time_zone": "America/Los_Angeles"},
}
CALENDAR_WINDOW = {"time_min": "2026-11-23T00:00:00-08:00", "time_max": "2026-12-01T00:00:00-08:00"}


def calendar_preview(
    event_id: str, summary: str, scope: str, start: dict[str, str], end: dict[str, str]
) -> tuple[str, dict[str, object], str]:
    return (
        "google-calendar-event-preview",
        {
            "event": {
                "eventId": event_id,
                "summary": summary,
                "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
                "start": start,
                "end": end,
                "scope": f"GOOGLE_CALENDAR_EVENT_SCOPE_{scope}",
            }
        },
        summary,
    )


def _tana_example(
    name: str,
    args: dict[str, object],
    result: object,
    result_text: str,
    *,
    nodes: tuple[tuple[str, str], ...] = (),
    workspace: bool = False,
    expands: bool = True,
) -> ActionExample:
    summaries: dict[str, object] = {}
    if nodes:
        summaries["nodes"] = [{"nodeId": node_id, "name": label} for node_id, label in nodes]
    if workspace:
        summaries["workspace"] = {"workspaceId": "product", "name": "Product workspace"}
    live_label = nodes[0][1] if nodes else "Product workspace"
    return ActionExample(
        "tana",
        name,
        args,
        result,
        result_text,
        compact_card_expands=expands,
        live_preview=("tana-node-summaries", summaries, live_label) if summaries else None,
    )


_TASK = ("task-1", "Plan review")
_TASK_TAG = ("tag-1", "Task")
_STATUS = ("field-1", "Status")
_DONE = ("option-1", "Done")
_ACTIVE = ("option-2", "Active")


EXAMPLE_ENVIRONMENT = {"name": "review-box", "namespace": "agentplane", "uid": "environment-1"}
EXAMPLE_SESSION = "81000000-0000-4000-8000-000000000001"
EXAMPLE_COMMAND = "81000000-0000-4000-8000-000000000002"
ENVIRONMENT_VIEW = {
    "target": EXAMPLE_ENVIRONMENT,
    "configuration": {"template": "python"},
    "observed_state": {"operating_mode": "OPERATING_MODE_RUNNING", "readiness": {"state": "READINESS_STATE_READY"}},
}


# Authored examples, not defaults generated from schemas: show meaningful optional arguments and
# domain results. Names are also the gallery identities. The test checks these against the registry.
EXAMPLES = (
    ActionExample("environments", "list", {"limit": 12}, {"items": [ENVIRONMENT_VIEW]}, "Template python"),
    ActionExample("environments", "get", {"target": EXAMPLE_ENVIRONMENT}, ENVIRONMENT_VIEW, "Template python"),
    ActionExample(
        "environments",
        "resume",
        {"target": EXAMPLE_ENVIRONMENT, "resume_token": "recorded-token"},
        {"target": EXAMPLE_ENVIRONMENT, "outcome": "RESUME_OUTCOME_REQUESTED"},
        "Resume requested",
    ),
    ActionExample(
        "sessions",
        "create",
        {
            "target": EXAMPLE_ENVIRONMENT,
            "session_id": EXAMPLE_SESSION,
            "spec": {"harness": "HARNESS_CODEX", "model": "gpt-5.4", "cwd": "/workspace"},
            "additional_instructions": "Use the project changelog for context.",
            "setup_script": "git status --short",
        },
        {"session": {"session_id": EXAMPLE_SESSION, "status": {"materialization": "SESSION_MATERIALIZATION_WAITING"}}},
        "Session created · Waiting",
    ),
    ActionExample(
        "sessions",
        "get",
        {"session_id": EXAMPLE_SESSION},
        {
            "session_id": EXAMPLE_SESSION,
            "session": {"source": "dispatcher", "kind": "observed"},
            "history": {"source": "history", "kind": "unavailable", "error": {"message": "History unavailable"}},
        },
        "History unavailable",
    ),
    ActionExample(
        "sessions",
        "submit_input",
        {
            "target": EXAMPLE_ENVIRONMENT,
            "session_id": EXAMPLE_SESSION,
            "command_id": EXAMPLE_COMMAND,
            "text": "Review the deployment notes.",
        },
        {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER"}},
        "Queued for delivery",
    ),
    ActionExample(
        "sessions",
        "observe_input",
        {"session_id": EXAMPLE_SESSION, "command_id": EXAMPLE_COMMAND, "wait_seconds": 15},
        {
            "session_id": EXAMPLE_SESSION,
            "dispatch": {"source": "dispatcher", "kind": "observed"},
            "observation": {"source": "history", "kind": "input_confirmed", "gaps": []},
        },
        "Input confirmed",
    ),
    ActionExample(
        "sessions",
        "cancel_input",
        {"session_id": EXAMPLE_SESSION, "command_id": EXAMPLE_COMMAND},
        {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_CANCELLED_IN_DISPATCHER"}},
        "Cancelled before delivery",
    ),
    ActionExample(
        "sessions",
        "interrupt_turn",
        {
            "target": EXAMPLE_ENVIRONMENT,
            "session_id": EXAMPLE_SESSION,
            "command_id": EXAMPLE_COMMAND,
            "turn_id": "turn-5",
        },
        {"dispatch": {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_RUNNER_ADMITTED"}},
        "Recorded by the runner",
    ),
    ActionExample(
        "sessions",
        "read_turns",
        {"session_id": EXAMPLE_SESSION, "count": 5},
        {
            "session_id": EXAMPLE_SESSION,
            "source": "history",
            "gaps": [{"first": "3", "last": "4"}],
            "turns": [
                {
                    "turn_id": "turn-5",
                    "input": [{"text": "Review the deployment notes."}],
                    "tool_calls": [{"name": "get_file_contents"}],
                    "status": "TURN_STATUS_SUCCEEDED",
                    "final_assistant_message": "The readiness checks are documented.",
                }
            ],
        },
        "Some history is missing.",
    ),
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
        "Saved as an unsent draft.",
        live_preview=(
            "gmail-thread-summaries",
            {"threads": [{"threadId": "thread-123", "subject": "Deployment correspondence"}]},
            "Deployment correspondence",
        ),
    ),
    ActionExample(
        "gmail",
        "threads_list",
        {"q": "from:deployments@example.com", "labelIds": ["INBOX"], "maxResults": 5, "includeSpamTrash": True},
        {
            "threads": [{"id": "thread-123", "snippet": "Deployment review requested"}],
            "resultSizeEstimate": 1,
            "nextPageToken": "test-continuation",
        },
        "More threads available.",
        compact_card_expands=False,
    ),
    ActionExample(
        "gmail",
        "threads_modify_labels",
        {"thread_ids": ["thread-123", "thread-456"], "add": ["Reviewed"], "remove": ["Needs review"]},
        {
            "thread_count": 2,
            "added": [{"id": "Label_17", "name": "Reviewed"}],
            "removed": [{"id": "Label_14", "name": "Needs review"}],
        },
        "Updated labels on 2 threads.",
        live_preview=(
            "gmail-thread-summaries",
            {"threads": [{"threadId": "thread-123", "subject": "Deployment review"}]},
            "Deployment review",
        ),
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
        '{"nodeId":"calendar-week-42"}',
        "Calendar node for week containing 2026-10-12",
        compact_card_expands=False,
        live_preview=(
            "tana-node-summaries",
            {"workspace": {"workspaceId": "workspace-123", "name": "Product workspace"}},
            "Product workspace",
        ),
    ),
    ActionExample(
        "tana",
        "move_node",
        {"nodeId": "draft", "targetNodeId": "website", "position": "start", "keepSourceReference": True},
        'Moved "Draft notes" from "Planning inbox" to "Website project" (start)\n\n'
        "Node ID: draft\n"
        "Previous parent: Planning inbox (inbox)\n"
        "New parent: Website project (website)\n"
        "Position: start",
        "Moved Draft notes from Planning inbox to Website project (start).",
        live_preview=(
            "tana-node-summaries",
            {"nodes": [{"nodeId": "draft", "name": "Draft notes"}, {"nodeId": "website", "name": "Website project"}]},
            "Website project",
        ),
    ),
    _tana_example(
        "read_node",
        {"nodeId": "task-1", "maxDepth": 2},
        "- [ ] Plan review <!-- node-id: task-1 -->\n  - Review the milestones <!-- node-id: child-1 -->",
        "Review the milestones",
        nodes=(_TASK,),
    ),
    _tana_example(
        "get_children",
        {"nodeId": "task-1", "offset": 1, "limit": 1},
        {
            "children": [
                {
                    "id": "child-1",
                    "name": "Milestone checklist",
                    "tags": [{"id": "tag-1", "name": "Task"}],
                    "tagIds": ["tag-1"],
                    "childCount": 2,
                    "docType": "content",
                    "description": "Check the remaining milestones.",
                    "created": "2026-10-10T10:00:00.000Z",
                    "inTrash": False,
                }
            ],
            "total": 4,
            "hasMore": True,
        },
        "More child nodes available",
        nodes=(_TASK,),
    ),
    _tana_example(
        "open_node",
        {"nodeId": "task-1", "openType": "panel"},
        'Opened "Plan review" in Tana (panel)',
        "Opened Plan review in a new side panel",
        nodes=(_TASK,),
    ),
    _tana_example(
        "list_workspaces",
        {},
        [{"id": "product", "name": "Product workspace", "homeNodeId": "home-1"}],
        "1 loaded workspace",
        expands=False,
    ),
    _tana_example(
        "list_tags",
        {"workspaceId": "product", "limit": 25},
        [{"id": "tag-1", "name": "Task", "color": "blue"}, {"id": "tag-2", "name": "Milestone"}],
        "2 tags returned",
        workspace=True,
    ),
    _tana_example(
        "get_tag_schema",
        {"tagId": "tag-1", "includeEditInstructions": True, "includeInheritedFields": True},
        "# Tag definition: Task (id:tag-1)\n\n## Template Fields\n"
        "- **Status** (id:field-1):: Options = Active *[default]*\n"
        "  - Active (id:option-2) *[default]*\n  - Done (id:option-1)\n\n"
        "## Tag-Level Edit Instructions\nUpdate status after review.\n",
        "Options = Active",
        nodes=(_TASK_TAG,),
    ),
    _tana_example(
        "edit_node",
        {"nodeId": "task-1", "name": {"old_string": "Plan", "new_string": "Schedule"}},
        "Successfully edited node task-1\nName: Schedule review",
        "Edited Schedule review",
        nodes=(_TASK,),
    ),
    _tana_example(
        "trash_node",
        {"nodeId": "task-1"},
        'Moved "Plan review" to trash\n\nNode ID: task-1\nTrash ID: trash-1',
        "Moved Plan review to trash",
        nodes=(_TASK,),
    ),
    _tana_example("check_node", {"nodeId": "task-1"}, 'Checked "Plan review"', "Completed Plan review", nodes=(_TASK,)),
    _tana_example(
        "uncheck_node", {"nodeId": "task-1"}, 'Unchecked "Plan review"', "Reopened Plan review", nodes=(_TASK,)
    ),
    _tana_example(
        "tag",
        {"nodeId": "task-1", "action": "add", "tagIds": ["tag-1", "tag-2"]},
        'Adding tags on "Plan review" (task-1):\n✓ Task: Added tag "Task"\n✓ Milestone: Tag "Milestone" is already applied',
        'Tag "Milestone" is already applied',
        nodes=(_TASK, _TASK_TAG, ("tag-2", "Milestone")),
    ),
    _tana_example(
        "set_tag_checkbox",
        {
            "tagId": "tag-1",
            "showCheckbox": True,
            "doneStateMapping": {"fieldId": "field-1", "checkedValues": ["option-1"], "uncheckedValues": ["option-2"]},
        },
        '✅ Configured checkbox for tag "Task"\nCheckbox: enabled\nDone state mapping: configured',
        "Checkbox enabled for Task",
        nodes=(_TASK_TAG, _STATUS, _DONE, _ACTIVE),
    ),
    _tana_example(
        "create_tag",
        {
            "workspaceId": "product",
            "name": "Milestone",
            "description": "A checkpoint for a project.",
            "extendsTagIds": ["tag-1"],
            "showCheckbox": True,
        },
        '✅ Created tag "Milestone" extending Task\nTag ID: tag-2\nExtends: Task',
        "Created tag Milestone",
        nodes=(_TASK_TAG,),
        workspace=True,
    ),
    _tana_example(
        "add_field_to_tag",
        {
            "tagId": "tag-1",
            "name": "Priority",
            "dataType": "options",
            "options": ["Standard", "Urgent"],
            "defaultValue": "Standard",
        },
        '✅ Added field "Priority" to tag "Task"\nField ID: field-2\nData Type: options',
        "Added Priority (options) to Task",
        nodes=(_TASK_TAG,),
    ),
    _tana_example(
        "set_field_content",
        {"nodeId": "task-1", "attributeId": "field-2", "content": None},
        "Field cleared",
        "Field cleared",
        nodes=(_TASK, ("field-2", "Notes")),
    ),
    _tana_example(
        "set_field_option",
        {"nodeId": "task-1", "attributeId": "field-1", "optionId": "option-1", "mode": "append"},
        'Appended field to option "Done"',
        "Appended to field Done",
        nodes=(_TASK, _STATUS, _DONE),
    ),
    _tana_example(
        "import_tana_paste",
        {
            "parentNodeId": "task-1",
            "content": "%%tana%%\n- Project kickoff #Task\n  - Owner:: Product\n  - Prepare the brief",
        },
        'Successfully imported 1 node(s) under "Plan review"\n\nTarget container: container-1\n'
        'Created nodes:\n- kickoff-1 ("Project kickoff")',
        "Imported 1 node under Plan review",
        nodes=(_TASK,),
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
        "google_calendar",
        "create_event",
        {
            "summary": CALENDAR_SERIES["summary"],
            "calendar_id": CALENDAR_ID,
            "start": CALENDAR_START,
            "end": CALENDAR_END,
            "location": "Meeting room 2",
            "attendees": ["coordinator@example.test"],
            "reminders": [{"method": "email", "minutes_before_start": 15}],
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
        },
        CALENDAR_SERIES,
        "Facilities coordination",
    ),
    ActionExample(
        "google_calendar",
        "update_event",
        {
            "event_id": "facilities-series",
            "calendar_id": CALENDAR_ID,
            "summary": "Facilities coordination (rescheduled)",
            "location": "",
            "attendees": [],
            "reminders": [],
        },
        {**CALENDAR_SERIES, "summary": "Facilities coordination (rescheduled)", "location": None, "attendees": []},
        "Facilities coordination (rescheduled)",
        live_preview=calendar_preview(
            "facilities-series",
            "Facilities coordination",
            "SERIES",
            {"dateTime": "2026-11-23T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
            {"dateTime": "2026-11-23T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
        ),
    ),
    ActionExample(
        "google_calendar",
        "delete_event",
        {"event_id": "facilities-series_20261130T170000Z", "calendar_id": CALENDAR_ID},
        None,
        "Deletion confirmed.",
        live_preview=calendar_preview(
            "facilities-series_20261130T170000Z",
            "Facilities coordination",
            "OCCURRENCE",
            {"dateTime": "2026-11-30T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
            {"dateTime": "2026-11-30T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
        ),
    ),
    ActionExample(
        "google_calendar",
        "get_event",
        {"event_id": "office-closure", "calendar_id": CALENDAR_ID},
        CALENDAR_ALL_DAY,
        "All day",
        live_preview=calendar_preview(
            "office-closure", "Office closure", "SINGLE_EVENT", {"date": "2026-11-26"}, {"date": "2026-11-28"}
        ),
    ),
    ActionExample(
        "google_calendar",
        "list_events",
        {"calendar_id": CALENDAR_ID, **CALENDAR_WINDOW, "query": "facilities"},
        {
            "events": [CALENDAR_SERIES, CALENDAR_ALL_DAY],
            "summary": "Facilities calendar",
            "next_page_token": "next-page",
        },
        "More events are available.",
    ),
    ActionExample(
        "google_calendar",
        "list_event_instances",
        {"calendar_id": CALENDAR_ID, "recurring_event_id": "facilities-series", **CALENDAR_WINDOW},
        {
            "events": [
                {
                    **CALENDAR_SERIES,
                    "event_id": "facilities-series_20261123T170000Z",
                    "recurrence": [],
                    "html_link": "https://calendar.google.com/calendar/event?eid=facilities-series_20261123T170000Z",
                    "recurring_event_id": "facilities-series",
                },
                CALENDAR_OCCURRENCE,
            ]
        },
        "Cancelled",
        live_preview=calendar_preview(
            "facilities-series",
            "Facilities coordination",
            "SERIES",
            {"dateTime": "2026-11-23T09:00:00-08:00", "timeZone": "America/Los_Angeles"},
            {"dateTime": "2026-11-23T09:30:00-08:00", "timeZone": "America/Los_Angeles"},
        ),
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


async def _expect_live_preview(surface: Locator, example: ActionExample) -> None:
    if example.live_preview is None:
        return
    _, payload, label = example.live_preview
    if "workspace" in payload and "nodes" not in payload:
        await expect(surface.get_by_text(label).first).to_be_visible()
    else:
        await expect(surface.get_by_role("link", name=label, exact=True).first).to_be_visible()


@DESKTOP_AND_MOBILE
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example.key)
async def test_action_presentations(
    view: VisualPage, app: AgentplaneFixture, example: ActionExample, viewport: Viewport
) -> None:
    form_factor = "mobile" if viewport == MOBILE else "desktop"

    async def capture(checkpoint: str, target: Locator) -> None:
        await view.capture(checkpoint, target=target, label=f"{example.key} · {form_factor} · {checkpoint}")

    page = view.page
    registered = await page.evaluate("window.agentplaneVisual.actionRendererKeys()")
    keys = [item.key for item in EXAMPLES]
    assert len(keys) == len(set(keys)), "Duplicate Action visual example"
    assert set(keys) == set(registered), "Every registered Action renderer needs a named visual example"

    summaries: dict[str, object] | None = (
        {example.live_preview[0]: example.live_preview[1]} if example.live_preview else None
    )
    # Native results are stored directly. MCP results use the actual wire envelope.
    mcp = example.group not in {"environments", "sandbox", "sessions"}
    result = (
        example.result
        if not mcp
        else {"content": [], "structuredContent": {"result": None}, "_meta": {"fastmcp": {"wrap_result": True}}}
        if example.result is None
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
    await wait_for_stable(page)
    await capture("sidebar-collapsed", sidebar)
    disclosure = sidebar.get_by_role("button", name="Expand ", exact=False)
    if example.compact_card_expands:
        await disclosure.click()
        await _expect_live_preview(sidebar, example)
        await expect(sidebar.get_by_text("Details shown as recorded data", exact=True)).to_have_count(0)
        await wait_for_stable(page)
        await expect(sidebar.locator(".ui-code-block-placeholder")).to_have_count(0)
        await wait_for_stable(page)
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
    elif example.key == "tana.move_node":
        await expect(pending).to_contain_text(
            "Move Draft notes to the start of Website project, keeping the source reference."
        )
        for name, node_id in (("Draft notes", "draft"), ("Website project", "website")):
            node_link = pending.get_by_role("link", name=name, exact=True)
            await expect(node_link).to_have_attribute("href", f"https://app.tana.inc?nodeid={node_id}")
            await expect(node_link).to_have_attribute("target", "_blank")
            await expect(pending.get_by_text(node_id, exact=True)).to_have_count(0)
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
    await wait_for_stable(page)
    await capture("pending-detail", pending)

    receipt = page.locator("[data-action-visual=receipt]")
    await expect(receipt).to_be_visible()
    await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(receipt).to_contain_text(example.result_text)
    if example.group != "google_calendar":
        await _expect_live_preview(receipt, example)
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
    await wait_for_stable(page)
    await capture("detail-result", receipt)

    history = page.locator("[data-action-visual=history]")
    details = history.get_by_role("button", name="Receipt details", exact=True)
    await expect(details).to_have_attribute("aria-expanded", "false")
    await history.evaluate("element => element.scrollIntoView({block: 'end'})")
    await wait_for_stable(page)
    await capture("history", history)

    await details.click()
    await expect(details).to_have_attribute("aria-expanded", "true")
    await history.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(history).to_contain_text(example.result_text)
    if example.group != "google_calendar":
        await _expect_live_preview(history, example)
    await expect(history.locator(".ui-code-block-placeholder")).to_have_count(0)
    await wait_for_stable(page)
    await capture("history-expanded", history)

    if example.key == "google_calendar.delete_event":
        await expect(receipt.get_by_role("link", name="Facilities coordination", exact=True)).to_have_count(0)
        raw_switch = receipt.get_by_role("switch", name="Raw", exact=True)
        await raw_switch.press("Space")
        await expect(raw_switch).to_be_checked()
        await expect(receipt).to_contain_text('"event_id": "facilities-series_20261130T170000Z"')
        await expect(receipt).to_contain_text('"content": []')
        await expect(receipt).to_contain_text('"result": null')


if __name__ == "__main__":
    pytest_bazel.main()
