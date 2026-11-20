"""One named set of real-host screenshots per registered Action renderer."""

import base64
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
    mcp_text: str | None = None

    @property
    def key(self) -> str:
        return f"{self.group}.{self.name}"


CONFIG_MAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "settings", "namespace": "apps"},
    "data": {"mode": "safe"},
}
POD_YAML = """apiVersion: v1
kind: Pod
metadata:
  name: web-0
  namespace: apps
status:
  phase: Running
  conditions:
  - type: Ready
    status: "False"
  containerStatuses:
  - name: main
    ready: true
    restartCount: 0
  - name: worker
    ready: false
    restartCount: 2
    state:
      waiting:
        reason: CrashLoopBackOff
"""
POD_TABLE = (
    "NAMESPACE   APIVERSION   KIND   NAME    READY   STATUS             RESTARTS   AGE   LABELS\n"
    "apps        v1           Pod    web-0   1/2     CrashLoopBackOff   2          4m    app=web\n"
)
CONFIG_MAP_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: apps}\ndata: {mode: safe}\n"
EVENTS_YAML = """# The following events (YAML format) were found:
- InvolvedObject: {Kind: Pod, Name: web-0, apiVersion: v1}
  Namespace: apps
  Timestamp: 2026-10-12 03:01:00 +0000 UTC
  Type: Warning
  Reason: BackOff
  Message: Back-off restarting failed container
"""
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
ORCHESTRATION_SANDBOX = {"name": "review-box", "uid": "81000000-0000-4000-8000-000000000003"}
ORCHESTRATION_SESSION: dict[str, object] = {"sandbox": ORCHESTRATION_SANDBOX, "session_id": EXAMPLE_SESSION}
ORCHESTRATION_COMMAND = {**ORCHESTRATION_SESSION, "command_id": EXAMPLE_COMMAND}
QUEUED = {"session_id": EXAMPLE_SESSION, "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER"}
ORCHESTRATION_SPEC = {"harness": "HARNESS_CODEX", "model": "gpt-5.4", "cwd": "/workspace"}

ENVIRONMENT_VIEW = {
    "target": EXAMPLE_ENVIRONMENT,
    "configuration": {"template": "python"},
    "observed_state": {"operating_mode": "OPERATING_MODE_RUNNING", "readiness": {"state": "READINESS_STATE_READY"}},
}


GMAIL_MESSAGE = {
    "id": "demo-message",
    "threadId": "demo-thread",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Deployment approval"},
            {"name": "From", "value": "Release bot <deployments@example.com>"},
            {"name": "To", "value": "reviewer@example.com"},
            {"name": "Date", "value": "Thu, 19 Nov 2026 19:00:00 +0000"},
        ],
        "body": {"data": base64.urlsafe_b64encode(b"The staging deployment passed.").decode()},
    },
}
GMAIL_DRAFT = {"id": "demo-draft", "message": GMAIL_MESSAGE}
GMAIL_LABEL = {
    "id": "Label_19",
    "name": "Deployments/Reviewed",
    "type": "user",
    "threadsTotal": 3,
    "threadsUnread": 0,
    "labelListVisibility": "labelShowIfUnread",
    "messageListVisibility": "show",
}
GMAIL_FILTER = {
    "id": "demo-filter",
    "criteria": {
        "from": "deployments@example.com",
        "subject": "Staging",
        "negatedQuery": "is:starred",
        "hasAttachment": True,
        "size": 2000,
        "sizeComparison": "larger",
    },
    "action": {"addLabelIds": ["STARRED"], "removeLabelIds": ["INBOX"], "forward": "archive@example.com"},
}

GITHUB_REPO: dict[str, object] = {"owner": "example", "repo": "service"}
GITHUB_PULL = {**GITHUB_REPO, "pullNumber": 42}
GITHUB_ISSUE = {**GITHUB_REPO, "issue_number": 7}
GITHUB_PULL_ROW = {
    "number": 42,
    "title": "Document deployment checks",
    "state": "open",
    "draft": True,
    "merged": False,
    "html_url": "https://github.com/example/service/pull/42",
    "head": {"ref": "docs"},
    "base": {"ref": "devel"},
}
GITHUB_ISSUE_ROW = {
    "number": 7,
    "title": "Review staging readiness",
    "state": "OPEN",
    "html_url": "https://github.com/example/service/issues/7",
    "labels": ["deployments"],
    "assignees": ["test-reviewer"],
}

GITHUB_COMMIT = {
    "sha": "0123456789abcdef0123456789abcdef01234567",
    "html_url": "https://github.com/example/service/commit/0123456789abcdef0123456789abcdef01234567",
    "commit": {
        "message": "Document readiness checks",
        "author": {"name": "Test maintainer", "date": "2026-11-19T18:00:00Z"},
    },
}
GITHUB_RELEASE = {
    "id": 15,
    "tag_name": "v1.2.0",
    "name": "Rollout tools 1.2",
    "html_url": "https://github.com/example/service/releases/tag/v1.2.0",
    "published_at": "2026-11-19T18:00:00Z",
    "prerelease": False,
    "draft": False,
    "body": "Adds staging readiness and rollback checks.",
}
GITHUB_USER = {"login": "test-reviewer", "profile_url": "https://github.com/test-reviewer"}

_KEFIR_ENTRY = {
    "entry_id": 12,
    "product_id": 21,
    "product_name": "Kefir",
    "amount": 2,
    "qu_name": "Bottle",
    "location_name": "Fridge",
    "best_before_date": "2026-11-25",
    "purchased_date": "2026-11-19",
    "price": 3.2,
    "open": True,
    "note": "Cultured",
}
_KEFIR_CHANGE = {
    "kind": "ok",
    "product_name": "Kefir",
    "transaction_id": "tx-kefir",
    "qu_name": "Bottle",
    "location_name": "Fridge",
}
_KEFIR_ALERT = {"product_id": 21, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}


HA_SENSOR = {
    "entity_id": "sensor.greenhouse_temperature",
    "state": "21.5",
    "attributes": {"friendly_name": "Greenhouse temperature", "unit_of_measurement": "°C"},
}
HA_FAN = {"entity_id": "fan.greenhouse", "friendly_name": "Greenhouse fan", "state": "on"}

# Authored examples, not defaults generated from schemas: show meaningful optional arguments and
# domain results. Names are also the gallery identities. The test checks these against the registry.
EXAMPLES = (
    ActionExample(
        "home_assistant",
        "ha_call_service",
        {
            "domain": "fan",
            "service": "set_percentage",
            "entity_id": "fan.greenhouse",
            "data": {"percentage": 35},
            "wait": True,
        },
        {
            "success": True,
            "domain": "fan",
            "service": "set_percentage",
            "message": "Successfully executed fan.set_percentage",
            "entity_id": "fan.greenhouse",
            "result": [HA_FAN],
        },
        "Successfully executed fan.set_percentage",
    ),
    ActionExample(
        "home_assistant",
        "ha_call_event",
        {"event_type": "greenhouse_report", "data": {"summary": "Temperature check"}},
        {"success": True, "event_type": "greenhouse_report", "message": "Event greenhouse_report fired."},
        "Event greenhouse_report fired.",
    ),
    ActionExample(
        "home_assistant",
        "ha_bulk_control",
        {
            "selector": {"domain": "fan", "area_ids": ["greenhouse"]},
            "action": "off",
            "dry_run": True,
            "parallel": False,
        },
        {"success": True, "message": "Dry run · 2 targets matched", "results": []},
        "Dry run · 2 targets matched",
    ),
    ActionExample(
        "home_assistant",
        "ha_get_state",
        {"entity_id": "sensor.greenhouse_temperature"},
        {"data": HA_SENSOR, "metadata": {"timezone": "America/Los_Angeles"}},
        "21.5 °C",
    ),
    ActionExample(
        "home_assistant",
        "ha_get_entity",
        {"entity_id": "fan.greenhouse"},
        {
            "success": True,
            "entity_id": "fan.greenhouse",
            "entity_entry": {"entity_id": "fan.greenhouse", "name": "Greenhouse fan"},
        },
        "Greenhouse fan",
    ),
    ActionExample(
        "home_assistant",
        "ha_get_device",
        {"entity_id": "fan.greenhouse"},
        {
            "success": True,
            "device": {
                "device_id": "device-1",
                "name_by_user": "Greenhouse controller",
                "manufacturer": "Example",
                "model": "Airflow controller",
                "entities": [HA_FAN],
            },
        },
        "Greenhouse controller",
    ),
    ActionExample(
        "home_assistant",
        "ha_search",
        {"query": "greenhouse", "domain_filter": "fan", "limit": 12},
        {"entities": [HA_FAN], "count": 1, "total_matches": 1, "has_more": False},
        "Greenhouse fan",
    ),
    ActionExample(
        "home_assistant",
        "ha_get_overview",
        {"domains": ["sensor"], "include_state": True},
        {"domains": {"sensor": {"count": 1, "entities": [HA_SENSOR]}}, "system_summary": {"version": "2026.11"}},
        "Greenhouse temperature",
    ),
    ActionExample(
        "home_assistant",
        "ha_list_floors_areas",
        {},
        {
            "success": True,
            "floors": [{"name": "Ground floor", "areas": [{"name": "Greenhouse"}, {"name": "Workshop"}]}],
            "unassigned_areas": [],
        },
        "Greenhouse, Workshop",
    ),
    ActionExample(
        "home_assistant",
        "ha_list_services",
        {"domain": "fan"},
        {
            "success": True,
            "services": {"fan.set_percentage": {"name": "Set fan speed", "description": "Set the percentage speed."}},
        },
        "Set fan speed",
    ),
    ActionExample(
        "orchestration_v1",
        "launch_sandbox",
        {
            "name": "review-box",
            "template": "python",
            "launcher_grants": ["send_message"],
            "policy_sets": ["docs-read"],
            "wake_on_send": True,
            "idle_suspend_after_seconds": 1200,
        },
        {"sandbox": ORCHESTRATION_SANDBOX, "launcher_grants": ["send_message"]},
        "Send inbox messages",
    ),
    ActionExample(
        "orchestration_v1",
        "list_launch_presets",
        {},
        {
            "presets": {
                "docs": {
                    "title": "Documentation reviewer",
                    "launch": {"template": "python"},
                    "create_session": {"spec": ORCHESTRATION_SPEC},
                }
            }
        },
        "Documentation reviewer",
        compact_card_expands=False,
    ),
    ActionExample(
        "orchestration_v1",
        "create_session",
        {**ORCHESTRATION_SESSION, "spec": {**ORCHESTRATION_SPEC, "instructions": "Check the changelog."}},
        {"session_id": EXAMPLE_SESSION, "status": {"materialization": "SESSION_MATERIALIZATION_WAITING"}},
        "Session created · Waiting",
    ),
    ActionExample(
        "orchestration_v1",
        "get_session",
        ORCHESTRATION_SESSION,
        {
            "sandbox_ready": {"status": "True", "reason": "PodReady"},
            "session": {
                "session_id": EXAMPLE_SESSION,
                "runner": {"harness": "HARNESS_CODEX", "active_turn_id": "turn-5"},
            },
            "dispatches": [],
        },
        "Turn active",
    ),
    ActionExample(
        "orchestration_v1",
        "list_sessions",
        {"sandbox": ORCHESTRATION_SANDBOX},
        {
            "sessions": [
                {
                    "session_id": EXAMPLE_SESSION,
                    "runner": {"harness": "HARNESS_CODEX"},
                    "last_turn_status": "TURN_STATUS_SUCCEEDED",
                }
            ]
        },
        "Last turn · Succeeded",
    ),
    ActionExample(
        "orchestration_v1",
        "change_model",
        {**ORCHESTRATION_COMMAND, "model": "gpt-5.4-mini"},
        QUEUED,
        "Queued for delivery",
    ),
    ActionExample(
        "orchestration_v1",
        "change_reasoning_effort",
        {**ORCHESTRATION_COMMAND, "effort": "high"},
        QUEUED,
        "Queued for delivery",
    ),
    ActionExample(
        "orchestration_v1",
        "submit_input",
        {**ORCHESTRATION_COMMAND, "text": "Review the deployment notes."},
        QUEUED,
        "Queued for delivery",
    ),
    ActionExample(
        "orchestration_v1",
        "interrupt_turn",
        {**ORCHESTRATION_COMMAND, "turn_id": "turn-5"},
        QUEUED,
        "Queued for delivery",
    ),
    ActionExample(
        "orchestration_v1",
        "read_turns",
        {**ORCHESTRATION_SESSION, "count": 5},
        {
            "turns": [
                {
                    "turn_id": "turn-5",
                    "input": [{"text": "Check the deployment notes."}],
                    "tool_calls": [],
                    "final_assistant_message": "Checked the deployment notes.",
                }
            ],
            "resume_token": "recorded-token",
        },
        "Checked the deployment notes.",
    ),
    ActionExample(
        "orchestration_v1",
        "wait_for_turn",
        {**ORCHESTRATION_SESSION, "resume_token": "recorded-token", "wait_seconds": 20},
        {"completed": False, "resume_token": "next-token"},
        "No completion observed before the wait ended",
    ),
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
        POD_TABLE,
        "1/2 ready",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_create_or_update",
        {"resource": json.dumps(CONFIG_MAP)},
        "# The following resources (YAML) have been created or updated successfully\n"
        "- " + CONFIG_MAP_YAML.strip().replace("\n", "\n  ") + "\n",
        "Resources applied",
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
        "ConfigMap apps/settings",
        mcp_text=CONFIG_MAP_YAML,
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_list",
        {"apiVersion": "v1", "kind": "Pod", "namespace": "apps", "labelSelector": "app=web"},
        {
            "items": [
                {"Name": "web-0", "Namespace": "apps", "Ready": "1/2", "Status": "CrashLoopBackOff", "Restarts": 2}
            ]
        },
        "CrashLoopBackOff",
        mcp_text=POD_TABLE,
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
        POD_YAML,
        "Not ready",
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
        EVENTS_YAML,
        "BackOff",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_list",
        {"labelSelector": "app=web"},
        POD_TABLE,
        "1/2 ready",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "namespaces_list",
        {"fieldSelector": "status.phase=Active"},
        "APIVERSION   KIND        NAME       STATUS   AGE   LABELS\n"
        "v1           Namespace   app-team   Active   4m    kubernetes.io/metadata.name=app-team\n",
        "Active",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "projects_list",
        {},
        "- apiVersion: project.openshift.io/v1\n  kind: Project\n  metadata: {name: app-team}\n"
        "  status: {phase: Active}\n",
        "Project app-team",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "nodes_log",
        {"name": "worker-1", "query": "kubelet", "tailLines": 80},
        "2026-10-12T03:00:00Z Pod sync complete\n2026-10-12T03:00:01Z Node status reported\n",
        "Node status reported",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "nodes_stats_summary",
        {"name": "worker-1"},
        json.dumps(
            {
                "node": {
                    "nodeName": "worker-1",
                    "cpu": {"time": "2026-10-12T03:00:00Z", "usageNanoCores": 280_000_000},
                    "memory": {
                        "time": "2026-10-12T03:00:00Z",
                        "usageBytes": 536_870_912,
                        "workingSetBytes": 268_435_456,
                    },
                }
            }
        ),
        "0.28 cores",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "nodes_top",
        {"label_selector": "pool=workers"},
        "NAME       CPU(cores)   CPU(%)   MEMORY(bytes)   MEMORY(%)\n"
        "worker-1   280m         7%       512Mi           12%\n",
        "Memory 512Mi (12%)",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_top",
        {"namespace": "apps", "all_namespaces": False, "label_selector": "app=web"},
        "NAMESPACE   NAME    CPU(cores)   MEMORY(bytes)\napps        web-0   40m          192Mi\n",
        "Memory 192Mi",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "configuration_view",
        {"minified": True},
        "apiVersion: v1\nkind: Config\ncurrent-context: staging\n"
        "contexts:\n- name: staging\n  context: {cluster: staging-cluster, namespace: apps}\n",
        "staging-cluster",
        compact_card_expands=False,
    ),
    ActionExample(
        "kubernetes_admin",
        "pods_run",
        {"name": "preview-web", "namespace": "apps", "image": "registry.example.test/web:v1", "port": 8080},
        "# The following resources (YAML) have been created or updated successfully\n"
        "- apiVersion: v1\n  kind: Pod\n  metadata: {name: preview-web, namespace: apps}\n"
        "  status: {phase: Pending}\n"
        "- apiVersion: v1\n  kind: Service\n  metadata: {name: preview-web, namespace: apps}\n",
        "Pod applied",
    ),
    ActionExample(
        "kubernetes_admin",
        "resources_scale",
        {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "namespace": "apps", "scale": 0},
        "# Current resource scale (YAML) is below\n"
        "apiVersion: autoscaling/v1\nkind: Scale\nmetadata: {name: web, namespace: apps}\n"
        "spec: {replicas: 0}\nstatus: {replicas: 2}\n",
        "Observed replicas · 2",
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
        "Pushed to docs.",
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
        "Deleted file.",
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
        {"id": "4200", "url": "https://github.com/example/service/pull/42"},
        "Pull request updated.",
    ),
    ActionExample(
        "github",
        "update_pull_request_branch",
        {"owner": "example", "repo": "service", "pullNumber": 42, "expectedHeadSha": "0123456789abcdef"},
        "Pull request branch update is in progress",
        "Update queued",
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
        {
            "commit": {
                "sha": "0123456789abcdef0123456789abcdef01234567",
                "html_url": "https://github.com/example/service/commit/0123456789abcdef0123456789abcdef01234567",
            },
            "content": {
                "name": "deployment.md",
                "path": "docs/deployment.md",
                "sha": "abcdef0123456789abcdef0123456789abcdef01",
                "html_url": "https://github.com/example/service/blob/docs/docs/deployment.md",
            },
        },
        "Committed file changes.",
    ),
    ActionExample(
        "github",
        "create_branch",
        {"owner": "example", "repo": "service", "branch": "docs", "from_branch": "devel"},
        {"ref": "refs/heads/docs", "object": {"sha": "0123456789abcdef0123456789abcdef01234567", "type": "commit"}},
        "Created branch docs.",
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
        {"id": "4200", "html_url": "https://github.com/example/service/pull/42"},
        "Pull request created.",
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
            "total_count": 11,
            "incomplete_results": True,
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
        "github",
        "list_pull_requests",
        {**GITHUB_REPO, "state": "open", "base": "devel"},
        [GITHUB_PULL_ROW],
        "Document deployment checks",
    ),
    ActionExample(
        "github",
        "pull_request_read",
        {**GITHUB_PULL, "method": "get_review_comments", "perPage": 10},
        {
            "review_threads": [
                {
                    "id": "PRRT_test",
                    "is_resolved": False,
                    "is_outdated": False,
                    "total_count": 1,
                    "comments": [
                        {
                            "path": "docs/deploy.md",
                            "line": 12,
                            "author": "test-reviewer",
                            "body": "Please document the rollback.",
                            "html_url": "https://github.com/example/service/pull/42#discussion_r81",
                        }
                    ],
                }
            ],
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False, "hasPreviousPage": False},
        },
        "Please document the rollback.",
    ),
    ActionExample(
        "github",
        "merge_pull_request",
        {
            **GITHUB_PULL,
            "merge_method": "squash",
            "expectedHeadSha": "0123456789abcdef",
            "commit_title": "Document deployment checks",
        },
        {
            "sha": "0123456789abcdef0123456789abcdef01234567",
            "merged": True,
            "message": "Pull Request successfully merged",
        },
        "Pull request merged.",
    ),
    ActionExample(
        "github", "request_copilot_review", GITHUB_PULL, {"status": "requested"}, "Copilot review requested."
    ),
    ActionExample(
        "github",
        "pull_request_review_write",
        {**GITHUB_PULL, "method": "submit_pending", "event": "REQUEST_CHANGES", "body": "Add a rollback section."},
        "pending pull request review successfully submitted",
        "pending pull request review successfully submitted",
    ),
    ActionExample(
        "github",
        "add_comment_to_pending_review",
        {
            **GITHUB_PULL,
            "path": "docs/deploy.md",
            "subjectType": "LINE",
            "line": 12,
            "side": "RIGHT",
            "body": "Please indicate the previous revision.",
        },
        "pull request review comment successfully added to pending review",
        "pull request review comment successfully added to pending review",
    ),
    ActionExample(
        "github",
        "add_reply_to_pull_request_comment",
        {**GITHUB_PULL, "commentId": 81, "body": "Added the rollback instructions.", "reaction": "+1"},
        {
            "comment": {"id": "82", "url": "https://github.com/example/service/pull/42#discussion_r82"},
            "reaction": {"id": "91"},
        },
        "Reply and reaction posted",
    ),
    ActionExample(
        "github",
        "issue_read",
        {**GITHUB_ISSUE, "method": "get"},
        {
            "method": "get",
            "issue": {**GITHUB_ISSUE_ROW, "state": "open", "body": "Confirm readiness on the staging environment."},
        },
        "Confirm readiness on the staging environment.",
    ),
    ActionExample(
        "github",
        "list_issues",
        {
            **GITHUB_REPO,
            "state": "OPEN",
            "labels": ["deployments"],
            "field_filters": [{"field_name": "Priority", "value": "P1"}],
        },
        {"issues": [GITHUB_ISSUE_ROW], "totalCount": 4, "pageInfo": {"hasNextPage": True, "hasPreviousPage": False}},
        "More results available.",
    ),
    ActionExample(
        "github",
        "search_issues",
        {"query": "repo:example/service is:open label:deployments"},
        {"items": [GITHUB_ISSUE_ROW], "total_count": 1, "incomplete_results": False},
        "Review staging readiness",
    ),
    ActionExample(
        "github",
        "issue_write",
        {
            **GITHUB_ISSUE,
            "method": "update",
            "title": "Review staging readiness",
            "state": "closed",
            "state_reason": "completed",
            "labels": ["deployments"],
            "issue_fields": [{"field_name": "Priority", "field_option_name": "P1"}],
        },
        {"id": "7700", "url": "https://github.com/example/service/issues/7"},
        "Issue updated.",
    ),
    ActionExample(
        "github",
        "sub_issue_write",
        {**GITHUB_ISSUE, "method": "add", "sub_issue_id": 7800, "replace_parent": True},
        {
            "method": "add",
            "issue": {
                "number": 8,
                "title": "Document rollback checks",
                "html_url": "https://github.com/example/service/issues/8",
                "assignees": [],
            },
        },
        "Sub-issue linked.",
    ),
    ActionExample(
        "github",
        "add_issue_comment",
        {**GITHUB_ISSUE, "body": "Staging is ready for review.", "reaction": "rocket"},
        {
            "comment": {"id": "70", "url": "https://github.com/example/service/issues/7#issuecomment-70"},
            "reaction": {"id": "71"},
        },
        "Comment and reaction posted",
    ),
    ActionExample(
        "github",
        "update_issue_comment",
        {**GITHUB_REPO, "comment_id": 70, "body": "Staging and rollback checks both passed."},
        {"id": "70", "html_url": "https://github.com/example/service/issues/7#issuecomment-70"},
        "Comment updated",
    ),
    ActionExample(
        "github",
        "list_issue_fields",
        GITHUB_REPO,
        [
            {
                "id": "IF_priority",
                "name": "Priority",
                "data_type": "SINGLE_SELECT",
                "visibility": "PRIVATE",
                "options": [{"id": "IF_opt", "name": "P1", "color": "ORANGE"}],
            }
        ],
        "Options P1",
    ),
    ActionExample(
        "github",
        "list_issue_types",
        GITHUB_REPO,
        [{"id": 1, "name": "Bug", "description": "Unexpected application behavior", "is_enabled": True}],
        "Unexpected application behavior",
    ),
    ActionExample(
        "github",
        "get_label",
        {**GITHUB_REPO, "name": "deployments"},
        {"name": "deployments", "description": "Release and rollout work", "color": "0366d6"},
        "Release and rollout work",
    ),
    ActionExample(
        "github",
        "create_repository",
        {
            "name": "demo-rollout",
            "organization": "example",
            "private": True,
            "autoInit": True,
            "description": "Tools for staging rollout",
        },
        {"id": "13", "url": "https://github.com/example/demo-rollout"},
        "Repository created",
    ),
    ActionExample(
        "github",
        "fork_repository",
        {**GITHUB_REPO, "organization": "test-destination"},
        "Fork is in progress",
        "Fork accepted",
    ),
    ActionExample(
        "github",
        "get_file_contents",
        {**GITHUB_REPO, "path": "docs", "ref": "refs/heads/devel"},
        [
            {
                "name": "readiness.md",
                "path": "docs/readiness.md",
                "type": "file",
                "size": 120,
                "html_url": "https://github.com/example/service/blob/devel/docs/readiness.md",
            }
        ],
        "docs/readiness.md",
    ),
    ActionExample(
        "github",
        "get_commit",
        {**GITHUB_REPO, "sha": GITHUB_COMMIT["sha"], "detail": "full_patch"},
        {
            **GITHUB_COMMIT,
            "stats": {"additions": 1, "deletions": 0},
            "files": [
                {
                    "filename": "docs/readiness.md",
                    "status": "added",
                    "additions": 1,
                    "patch": "@@ -0,0 +1 @@\n+Check readiness.",
                }
            ],
        },
        "Check readiness.",
    ),
    ActionExample(
        "github",
        "list_commits",
        {**GITHUB_REPO, "sha": "devel", "path": "docs", "perPage": 5},
        [GITHUB_COMMIT],
        "Document readiness checks",
    ),
    ActionExample(
        "github",
        "list_branches",
        {**GITHUB_REPO, "perPage": 5},
        [
            {"name": "devel", "sha": GITHUB_COMMIT["sha"], "protected": True},
            {"name": "docs", "sha": GITHUB_COMMIT["sha"], "protected": False},
        ],
        "Protected",
    ),
    ActionExample("github", "list_tags", GITHUB_REPO, [{"name": "v1.2.0", "sha": GITHUB_COMMIT["sha"]}], "v1.2.0"),
    ActionExample(
        "github",
        "get_tag",
        {**GITHUB_REPO, "tag": "v1.2.0"},
        {"ref": "refs/tags/v1.2.0", "object": {"type": "commit", "sha": GITHUB_COMMIT["sha"]}},
        "Lightweight tag",
    ),
    ActionExample("github", "list_releases", {**GITHUB_REPO, "perPage": 5}, [GITHUB_RELEASE], "Rollout tools 1.2"),
    ActionExample("github", "get_latest_release", GITHUB_REPO, GITHUB_RELEASE, "Adds staging readiness"),
    ActionExample(
        "github", "get_release_by_tag", {**GITHUB_REPO, "tag": "v1.2.0"}, GITHUB_RELEASE, "Rollout tools 1.2"
    ),
    ActionExample(
        "github",
        "search_commits",
        {"query": "repo:example/service readiness", "perPage": 5},
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [{**GITHUB_COMMIT, "repository": {"full_name": "example/service"}}],
        },
        "Document readiness checks",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "get_me",
        {},
        {**GITHUB_USER, "details": {"name": "Test reviewer", "company": "Example organization"}},
        "Test reviewer",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "get_team_members",
        {"org": "example", "team_slug": "release-reviewers"},
        ["test-reviewer"],
        "test-reviewer",
    ),
    ActionExample(
        "github",
        "get_teams",
        {"user": "test-reviewer"},
        [
            {
                "org": "example",
                "teams": [
                    {"name": "Release reviewers", "slug": "release-reviewers", "description": "Review staging changes"}
                ],
            }
        ],
        "Release reviewers",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "list_repository_collaborators",
        {**GITHUB_REPO, "affiliation": "direct"},
        {
            "firstPage": 1,
            "lastPage": 3,
            "nextPage": 2,
            "prevPage": 0,
            "items": [{"login": "test-reviewer", "id": 8, "role_name": "triage"}],
        },
        "Triage",
    ),
    ActionExample(
        "github",
        "search_code",
        {"query": "repo:example/service readiness", "perPage": 5},
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [
                {
                    "name": "readiness.md",
                    "path": "docs/readiness.md",
                    "sha": GITHUB_COMMIT["sha"],
                    "repository": "example/service",
                    "text_matches": [{"fragment": "Wait for staging readiness."}],
                }
            ],
        },
        "Wait for staging readiness.",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "search_repositories",
        {"query": "org:example rollout", "minimal_output": True},
        {
            "total_count": 1,
            "incomplete_results": False,
            "items": [
                {
                    "id": 10,
                    "name": "service",
                    "full_name": "example/service",
                    "html_url": "https://github.com/example/service",
                    "description": "Staging rollout tools",
                    "language": "Go",
                    "stargazers_count": 12,
                }
            ],
        },
        "Staging rollout tools",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "search_users",
        {"query": "org:example reviewer"},
        {"total_count": 1, "incomplete_results": False, "items": [GITHUB_USER]},
        "test-reviewer",
        compact_card_expands=False,
    ),
    ActionExample(
        "github",
        "actions_get",
        {**GITHUB_REPO, "method": "get_workflow_job", "resource_id": "208"},
        {
            "method": "get_workflow_job",
            "workflow_job": {
                "id": 208,
                "run_id": 81,
                "name": "Check staging readiness",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/example/service/actions/runs/81/job/208",
                "steps": [{"name": "Verify readiness", "number": 1, "status": "completed", "conclusion": "failure"}],
            },
        },
        "Verify readiness",
    ),
    ActionExample(
        "github",
        "actions_list",
        {
            **GITHUB_REPO,
            "method": "list_workflow_runs",
            "resource_id": "rollout.yml",
            "workflow_runs_filter": {"branch": "devel", "status": "completed"},
        },
        {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 81,
                    "name": "Rollout checks",
                    "display_title": "Check staging deployment",
                    "workflow_id": 4,
                    "run_number": 5,
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "devel",
                    "html_url": "https://github.com/example/service/actions/runs/81",
                }
            ],
        },
        "Check staging deployment",
    ),
    ActionExample(
        "github",
        "actions_run_trigger",
        {**GITHUB_REPO, "method": "rerun_failed_jobs", "run_id": 81},
        {
            "message": "Failed jobs have been queued for re-run",
            "run_id": 81,
            "status": "201 Created",
            "status_code": 201,
        },
        "Rerun queued",
    ),
    ActionExample(
        "github",
        "get_job_logs",
        {**GITHUB_REPO, "run_id": 81, "failed_only": True, "return_content": True, "tail_lines": 25},
        {
            "run_id": 81,
            "failed_jobs": 1,
            "total_jobs": 2,
            "message": "Retrieved logs for 1 failed jobs",
            "logs": [
                {
                    "job_id": 208,
                    "job_name": "Check staging readiness",
                    "logs_content": "The health endpoint returned 503.",
                    "message": "Log content",
                    "original_length": 100,
                }
            ],
            "return_format": {"content": True, "urls": False},
        },
        "The health endpoint returned 503.",
    ),
    ActionExample(
        "github",
        "run_secret_scanning",
        {**GITHUB_REPO, "files": ["EXAMPLE_CONFIG=stub", "EXAMPLE_TOKEN=fixture-only"]},
        "Hosted scanner receipt fixture",
        "View submitted content and scan findings in Raw.",
    ),
    ActionExample(
        "github",
        "ui_get",
        {**GITHUB_REPO, "method": "reviewers"},
        {
            "method": "reviewers",
            "reviewers": {
                "users": [{"login": "test-reviewer", "avatar_url": ""}],
                "teams": [{"org": "example", "name": "Release reviewers", "slug": "release-reviewers"}],
                "totalCount": 2,
                "has_more": False,
            },
        },
        "Release reviewers",
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
    ActionExample("gmail", "drafts_get", {"id": GMAIL_DRAFT["id"]}, GMAIL_DRAFT, "Unsent draft"),
    ActionExample(
        "gmail",
        "drafts_list",
        {"q": "subject:deployment", "maxResults": 10},
        {
            "drafts": [{"id": GMAIL_DRAFT["id"], "message": {"id": GMAIL_MESSAGE["id"], "threadId": "demo-thread"}}],
            "resultSizeEstimate": 1,
            "nextPageToken": "opaque-drafts-page",
        },
        "More drafts available.",
        compact_card_expands=False,
    ),
    ActionExample(
        "gmail",
        "drafts_update",
        {
            "draft_id": GMAIL_DRAFT["id"],
            "to": ["release-reviewer@example.com"],
            "subject": "Revised deployment approval",
            "body": "Please approve the revised deployment.",
        },
        {"id": GMAIL_DRAFT["id"], "message": {"id": "updated-message"}},
        "Saved as an unsent draft.",
    ),
    ActionExample("gmail", "drafts_delete", {"draft_id": GMAIL_DRAFT["id"]}, None, "Unsent draft deleted."),
    ActionExample(
        "gmail",
        "threads_get",
        {"id": "demo-thread", "format": "full"},
        {"id": "demo-thread", "messages": [GMAIL_MESSAGE]},
        "The staging deployment passed.",
    ),
    ActionExample(
        "gmail",
        "messages_get",
        {"id": GMAIL_MESSAGE["id"], "format": "metadata", "metadataHeaders": ["Subject", "From"]},
        {
            "id": GMAIL_MESSAGE["id"],
            "threadId": "demo-thread",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Deployment notice metadata"},
                    {"name": "From", "value": "Release bot <deployments@example.com>"},
                ]
            },
        },
        "Deployment notice metadata",
    ),
    ActionExample(
        "gmail",
        "labels_list",
        {},
        {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}, GMAIL_LABEL]},
        "Deployments/Reviewed",
        compact_card_expands=False,
    ),
    ActionExample("gmail", "labels_get", {"id": GMAIL_LABEL["id"]}, GMAIL_LABEL, "3 total, 0 unread"),
    ActionExample(
        "gmail",
        "labels_create",
        {"name": GMAIL_LABEL["name"], "label_list_visibility": "labelShowIfUnread"},
        GMAIL_LABEL,
        "Gmail created the label.",
        compact_card_expands=False,
    ),
    ActionExample(
        "gmail",
        "labels_patch",
        {"label_id": GMAIL_LABEL["id"], "label_list_visibility": "labelShowIfUnread"},
        GMAIL_LABEL,
        "Gmail saved the label changes.",
        live_preview=(
            "gmail-resource-summaries",
            {"labels": [{"id": GMAIL_LABEL["id"], "title": GMAIL_LABEL["name"]}]},
            "Deployments/Reviewed",
        ),
    ),
    ActionExample("gmail", "labels_delete", {"label_id": GMAIL_LABEL["id"]}, None, "Label deleted."),
    ActionExample(
        "gmail", "filters_list", {}, {"filter": [GMAIL_FILTER]}, "archive@example.com", compact_card_expands=False
    ),
    ActionExample("gmail", "filters_get", {"id": GMAIL_FILTER["id"]}, GMAIL_FILTER, "archive@example.com"),
    ActionExample(
        "gmail",
        "filters_create",
        {"criteria": GMAIL_FILTER["criteria"], "action": GMAIL_FILTER["action"]},
        GMAIL_FILTER,
        "Gmail created the filter.",
    ),
    ActionExample("gmail", "filters_delete", {"filter_id": GMAIL_FILTER["id"]}, None, "Filter deleted."),
    ActionExample(
        "grocy_sf",
        "products_list",
        {"detail": "full"},
        [{"id": 1, "name": "Olive Oil", "min_stock_amount": 1}, {"id": 2, "name": "Oats"}],
        "Minimum 1 in stock",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "quantity_units_list",
        {"detail": "full"},
        [{"id": 1, "name": "Bottle", "name_plural": "Bottles"}, {"id": 2, "name": "Gram", "name_plural": "Grams"}],
        "Plural: Bottles",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "get_system_info",
        {},
        {"grocy_version": {"Version": "4.5.0"}, "php_version": "8.4", "sqlite_version": "3.50", "db_version": 1},
        "Grocy 4.5.0",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "products_edit",
        {"items": [{"product": "Oats", "name": "Rolled oats", "location": "Pantry", "clear_fields": ["description"]}]},
        [{"kind": "ok", "object_id": 2}],
        "1 product edited",
    ),
    ActionExample(
        "grocy_sf",
        "stock_get",
        {"products": ["Kefir"], "locations": ["Fridge"]},
        [
            {
                "product_id": 21,
                "product_name": "Kefir",
                "amount": 3,
                "amount_opened": 1,
                "qu_name": "Bottle",
                "location_name": "Fridge",
                "best_before_date": "2026-11-25",
            }
        ],
        "1 opened · Best before 2026-11-25",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "stock_add",
        {"items": [{"product": "Kefir", "location": "Fridge", "qu": "Bottle", "amount": 2, "price": 3.2}]},
        [{**_KEFIR_CHANGE, "amount_delta": 2, "new_amount": 4, "entry_id": 12, "best_before_date": "2026-11-25"}],
        "Now 4 Bottle total",
    ),
    ActionExample(
        "grocy_sf",
        "stock_consume",
        {"items": [{"product": "Kefir", "location": "Fridge", "qu": "Bottle", "amount": 1, "spoiled": True}]},
        [{**_KEFIR_CHANGE, "amount_delta": -1, "new_amount": 1}],
        "Change · -1 Bottle",
    ),
    ActionExample(
        "grocy_sf",
        "stock_set",
        {"items": [{"product": "Kefir", "location": "Fridge", "new_amount": 0}]},
        [{**_KEFIR_CHANGE, "amount_delta": -2, "new_amount": 0}],
        "Now 0 Bottle total",
    ),
    ActionExample(
        "grocy_sf",
        "stock_transfer",
        {"product": "Kefir", "qu": "Bottle", "amount": 1, "from_location": "Fridge", "to_location": "Cellar"},
        {**_KEFIR_CHANGE, "location_name": "Fridge -> Cellar"},
        "Kefir · Fridge → Cellar",
    ),
    ActionExample(
        "grocy_sf",
        "stock_entries_list",
        {"products": ["Kefir"]},
        [{"kind": "ok", "entry": _KEFIR_ENTRY}],
        "Kefir · 2 Bottle in Fridge",
    ),
    ActionExample(
        "grocy_sf",
        "stock_entry_edit",
        {"items": [{"entry_id": 12, "amount": 2, "clear_fields": ["price"]}]},
        [
            {
                "kind": "ok",
                "entry": {**_KEFIR_ENTRY, "price": None},
                "changes": {"amount": {"old": 3, "new": 2}, "price": {"old": 3.2, "new": None}},
            }
        ],
        "Amount · 3 → 2",
    ),
    ActionExample(
        "grocy_sf",
        "get_product_stock",
        {"productId": 21},
        {
            "product": {"id": "21", "name": "Kefir"},
            "stock_amount": "2",
            "stock_amount_opened": "1",
            "quantity_unit_stock": {"name": "Bottle", "name_plural": "Bottles"},
            "location": {"name": "Fridge"},
            "next_due_date": "2026-11-25",
        },
        "Kefir · 2 Bottles",
    ),
    ActionExample(
        "grocy_sf",
        "get_expiring_stock",
        {"days_ahead": 7},
        [{**_KEFIR_ALERT, "location_name": "Fridge", "best_before_date": "2026-11-25", "days_until_expiry": 6}],
        "Due in 6 d",
    ),
    ActionExample(
        "grocy_sf",
        "get_expired_stock",
        {},
        [{**_KEFIR_ALERT, "location_name": "Fridge", "best_before_date": "2026-11-17", "days_overdue": 2}],
        "2 d overdue",
    ),
    ActionExample(
        "grocy_sf",
        "get_below_minimum_stock",
        {},
        [{**_KEFIR_ALERT, "min_amount": 3, "deficit": 1}],
        "Minimum 3 · 1 short",
    ),
    ActionExample(
        "grocy_sf",
        "list_volatile_stock",
        {"due_soon_days": 6},
        {
            "due_products": [
                {"product_id": "21", "amount": "2", "best_before_date": "2026-11-25", "product": {"name": "Kefir"}}
            ],
            "overdue_products": [],
            "expired_products": [],
            "missing_products": [{"id": "22", "name": "Oats", "amount_missing": "2"}],
        },
        "Oats · 2 stock units short",
    ),
    ActionExample(
        "grocy_sf",
        "open_product_stock",
        {"productId": 21, "amount": 1, "stock_entry_id": "batch-kefir"},
        [{"product_id": "21", "amount": "1", "transaction_id": "tx-open", "transaction_type": "product-opened"}],
        "Marked 1 stock units as opened",
    ),
    ActionExample("grocy_sf", "transaction_undo", {"transactionId": "tx-kefir"}, "null", "Transaction undone"),
    ActionExample(
        "grocy_sf",
        "shopping_list_get",
        {"shopping_list": "Weekly groceries"},
        {
            "name": "Weekly groceries",
            "items": [
                {
                    "item_id": 52,
                    "product_id": 21,
                    "product_name": "Kefir",
                    "amount": 2,
                    "qu_name": "Bottle",
                    "note": "Plain",
                    "done": True,
                }
            ],
        },
        "Kefir · Done",
    ),
    ActionExample(
        "grocy_sf",
        "shopping_list_clear",
        {"shopping_list": "Weekly groceries"},
        {"kind": "ok", "cleared": 4},
        "4 items removed · Shopping list kept",
    ),
    ActionExample(
        "grocy_sf",
        "shopping_list_items_add",
        {"items": [{"shopping_list": "Weekly groceries", "product": "Kefir", "amount": 2, "note": "Plain"}]},
        [{"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}],
        "1 item added",
    ),
    ActionExample(
        "grocy_sf",
        "shopping_list_items_remove",
        {"item_ids": [52]},
        [{"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}],
        "1 item removed",
    ),
    ActionExample(
        "grocy_sf",
        "shopping_list_item_edit",
        {"item_id": 52, "done": True, "clear_fields": ["note"]},
        {"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"},
        "1 item updated",
    ),
    ActionExample(
        "grocy_sf",
        "shopping_lists_list",
        {"detail": "full"},
        [{"id": 3, "name": "Weekly groceries", "description": "Regular restock"}],
        "Weekly groceries · Regular restock",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "shopping_lists_create",
        {"items": [{"name": "Farmers market", "description": "Saturday"}]},
        [{"kind": "ok", "created_object_id": 3}],
        "Created Farmers market",
    ),
    ActionExample(
        "grocy_sf",
        "products_create",
        {
            "items": [
                {
                    "name": "Kefir",
                    "location": "Fridge",
                    "stock_qu": "Bottle",
                    "due_type": 2,
                    "default_best_before_days": 7,
                }
            ]
        },
        [{"kind": "ok", "created_object_id": 21}],
        "Created Kefir",
    ),
    ActionExample(
        "grocy_sf",
        "product_delete",
        {"product": "Old coffee"},
        {"kind": "ok", "object_id": 31},
        "Deleted Old coffee and its stock entries",
    ),
    ActionExample(
        "grocy_sf",
        "products_merge",
        {"productIdToKeep": 16, "productIdToRemove": 15},
        "null",
        "Products merged",
        live_preview=(
            "grocy-reference-summaries",
            {"references": [{"key": "products/16", "name": "Ground coffee"}]},
            "Ground coffee",
        ),
    ),
    ActionExample(
        "grocy_sf",
        "locations_create",
        {"items": [{"name": "Chest freezer", "is_freezer": True}]},
        [{"kind": "ok", "created_object_id": 4}],
        "Created Chest freezer",
    ),
    ActionExample(
        "grocy_sf",
        "locations_list",
        {"detail": "full"},
        [{"id": 4, "name": "Chest freezer", "is_freezer": "1"}],
        "Chest freezer · Freezer",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "product_groups_create",
        {"items": [{"name": "Breakfast", "description": "Morning staples"}]},
        [{"kind": "ok", "created_object_id": 5}],
        "Created Breakfast",
    ),
    ActionExample(
        "grocy_sf",
        "product_groups_list",
        {"detail": "full"},
        [{"id": 5, "name": "Breakfast", "description": "Morning staples"}],
        "Breakfast · Morning staples",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "quantity_units_create",
        {"items": [{"name": "Bottle", "name_plural": "Bottles"}]},
        [{"kind": "ok", "created_object_id": 6}],
        "Created Bottle",
    ),
    ActionExample(
        "grocy_sf",
        "entities_create",
        {"items": [{"entity_type": "tasks", "body": {"name": "Clean fridge", "due_date": "2026-11-22"}}]},
        [{"kind": "ok", "created_object_id": 41}],
        "Created Clean fridge",
    ),
    ActionExample(
        "grocy_sf",
        "entities_get",
        {"entity_type": "product_groups", "object_ids": [5]},
        [
            {
                "kind": "ok",
                "entity_type": "product_groups",
                "object_id": 5,
                "data": {"id": "5", "name": "Breakfast", "description": "Morning staples"},
            }
        ],
        "Description · Morning staples",
    ),
    ActionExample(
        "grocy_sf",
        "entities_list",
        {"entity_types": ["quantity_units", "product_groups"]},
        {
            "quantity_units": [{"id": "6", "name": "Bottle", "name_plural": "Bottles"}],
            "product_groups": [{"id": "5", "name": "Breakfast"}],
        },
        "Plural · Bottles",
    ),
    ActionExample("grocy_sf", "entity_delete", {"entity": "product_groups", "objectId": 5}, "null", "Record deleted"),
    ActionExample(
        "grocy_sf",
        "entity_update",
        {"entity": "product_groups", "objectId": 5, "body": {"description": "Pantry basics"}},
        "null",
        "Record updated",
    ),
    ActionExample(
        "grocy_sf",
        "file_get",
        {"group": "productpictures", "fileName": "a2VmaXIucG5n", "force_serve_as": "picture", "best_fit_width": 320},
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9iUAAAAASUVORK5CYII=",
        "File contents returned",
    ),
    ActionExample(
        "grocy_sf",
        "file_upload",
        {"group": "productpictures", "fileName": "a2VmaXIucG5n", "body": "aW1hZ2UgYnl0ZXM="},
        "null",
        "File uploaded",
    ),
    ActionExample(
        "grocy_sf",
        "get_current_user",
        {},
        {"id": 4, "display_name": "Casey Example", "username": "casey"},
        "Casey Example · casey",
        compact_card_expands=False,
    ),
    ActionExample(
        "grocy_sf",
        "get_db_changed_time",
        {},
        {"changed_time": "2026-11-19 18:20:13"},
        "Grocy database last changed · 2026-11-19 18:20:13",
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
        "sandbox",
        "get_template",
        {"template": "python"},
        {
            "spec": {
                "containers": [{"name": "workspace", "image": "example.com/sandbox/python:3.13"}],
                "volumes": [{"name": "work"}],
            }
        },
        "example.com/sandbox/python:3.13",
        compact_card_expands=False,
    ),
    ActionExample(
        "ssh",
        "list_targets",
        {},
        [
            {"user": "backup", "host": "archive.example.com", "available": True},
            {
                "user": "build",
                "host": "builder.example.com",
                "available": False,
                "error": {"message": "Configured SSH identity is unavailable."},
            },
        ],
        "backup@archive.example.com",
        compact_card_expands=False,
    ),
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
    if example.group == "grocy_sf" and summaries is None:
        summaries = {"grocy-reference-summaries": {"references": []}}
    # Native results are stored directly. MCP results use the actual wire envelope.
    mcp = example.group not in {"environments", "orchestration_v1", "sandbox", "sessions"}
    result = (
        example.result
        if not mcp
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
    await wait_for_stable(page)
    await capture("pending-detail", pending)

    receipt = page.locator("[data-action-visual=receipt]")
    await expect(receipt).to_be_visible()
    await receipt.evaluate("element => element.scrollIntoView({block: 'end'})")
    await expect(receipt).to_contain_text(example.result_text)
    if example.group not in {"google_calendar", "grocy_sf"}:
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
    if example.group not in {"google_calendar", "grocy_sf"}:
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
