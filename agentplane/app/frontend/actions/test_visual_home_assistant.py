"""home_assistant Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

HA_SENSOR = {
    "entity_id": "sensor.greenhouse_temperature",
    "state": "21.5",
    "attributes": {"friendly_name": "Greenhouse temperature", "unit_of_measurement": "°C"},
}

HA_FAN = {"entity_id": "fan.greenhouse", "friendly_name": "Greenhouse fan", "state": "on"}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "home_assistant",
            "ha_eval_template",
            {"template": "{{ 12 + 5 }}", "timeout": 3},
            {"success": True, "template": "{{ 12 + 5 }}", "result": "17"},
            "17",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_app",
            {"source": "installed", "query": "weather"},
            {
                "success": True,
                "addons": [
                    {
                        "name": "Weather Bridge",
                        "slug": "weather_bridge",
                        "state": "started",
                        "version": "1.4.0",
                        "installed": True,
                    }
                ],
                "summary": {"total_installed": 1},
            },
            "Weather Bridge",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_blueprint",
            {"domain": "automation"},
            {
                "success": True,
                "blueprints": [
                    {
                        "path": "sample/study.yaml",
                        "name": "Study lighting",
                        "description": "Turn on a desk lamp at dusk",
                    }
                ],
            },
            "Study lighting",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_get_hacs_info",
            {"action": "search", "query": "study", "installed_only": False},
            {
                "success": True,
                "query": "study",
                "count": 1,
                "has_more": False,
                "results": [
                    {
                        "name": "Study Lighting",
                        "full_name": "example/study-lighting",
                        "installed_version": "1.0.0",
                        "available_version": "1.1.0",
                    }
                ],
            },
            "Study Lighting",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_skill_guide",
            {"skill": "home-assistant-best-practices", "file": "SKILL.md"},
            {
                "success": True,
                "skill": "home-assistant-best-practices",
                "file": "SKILL.md",
                "content": "# Automation review\nReview triggers before saving.",
            },
            "Review triggers before saving",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_import_blueprint",
            {"url": "https://example.test/blueprints/study.yaml", "overwrite": False},
            {
                "success": True,
                "message": "Successfully imported blueprint",
                "url": "https://example.test/blueprints/study.yaml",
                "imported_blueprint": {"name": "Study lighting", "path": "sample/study.yaml"},
                "overrides_existing": False,
            },
            "Study lighting",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_app",
            {"slug": "weather_bridge", "action": "start"},
            {
                "success": True,
                "message": "App (add-on) weather_bridge start completed.",
                "action": "start",
                "slug": "weather_bridge",
            },
            "start completed",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_backup",
            {"scope": "edits", "action": "list", "domain": "automation", "limit": 5},
            {
                "success": True,
                "data": {
                    "backups": [
                        {
                            "backup_name": "automation-study-2026-10-12.json",
                            "domain": "automation",
                            "entity_id": "automation.study",
                        }
                    ],
                    "count": 1,
                },
            },
            "automation.study",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_energy_prefs",
            {"mode": "add_device", "stat_consumption": "sensor.desk_energy"},
            {
                "success": True,
                "message": "Energy device added",
                "mode": "add_device",
                "target_key": "device_consumption",
                "new_count": 1,
            },
            "Energy device added",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_hacs",
            {"action": "add_repository", "repository": "example/study-lighting", "category": "integration"},
            {
                "success": True,
                "message": "Registered example/study-lighting",
                "data": {"name": "Study Lighting", "full_name": "example/study-lighting"},
            },
            "Registered example/study-lighting",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_pipeline",
            {"action": "list"},
            {
                "success": True,
                "operation": "list",
                "pipelines": [{"id": "pipeline-study", "name": "Study voice", "language": "en"}],
                "preferred_pipeline": "pipeline-study",
            },
            "Study voice",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_radio",
            {"radio": "zigbee", "action": "network_status"},
            {
                "success": True,
                "radio": "zigbee",
                "action": "network_status",
                "data": {"status": "online", "devices": [{"name": "Study sensor"}]},
            },
            "Online",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_theme",
            {"action": "set", "theme_name": "Slate", "mode": "dark"},
            {
                "success": True,
                "data": {"theme": "Slate", "mode": "dark", "default_theme": "default", "default_dark_theme": "Slate"},
            },
            "Slate",
        ),
        ActionExample(
            "home_assistant",
            "ha_manage_updates",
            {"action": "list", "categories": ["addons"], "include_skipped": False},
            {
                "success": True,
                "updates_available": 1,
                "skipped_count": 0,
                "updates": [
                    {
                        "entity_id": "update.weather_bridge",
                        "name": "Weather Bridge",
                        "installed_version": "1.0.0",
                        "latest_version": "1.1.0",
                    }
                ],
            },
            "Weather Bridge",
        ),
        ActionExample(
            "home_assistant",
            "ha_reload_core",
            {"target": "automation"},
            {
                "success": True,
                "message": "Successfully reloaded automation",
                "target": "automation",
                "service": "automation.reload",
            },
            "Successfully reloaded automation",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_report_issue",
            {"tool_call_count": 2},
            {
                "success": True,
                "suggested_title": "Intermittent automation refresh failure",
                "log_count": 4,
                "formatted_report": "## Diagnostic summary\nAn example integration reported a refresh timeout.",
            },
            "Intermittent automation refresh failure",
        ),
        ActionExample(
            "home_assistant",
            "ha_restart",
            {"confirm": True},
            {
                "success": True,
                "message": "Home Assistant restart initiated. The system will be unavailable for 1-5 minutes.",
                "warnings": ["Connection will be lost during restart."],
            },
            "Restart initiated",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_automation",
            {"identifier": "automation.study_lighting"},
            {
                "success": True,
                "action": "get",
                "automation_id": "automation.study_lighting",
                "config": {
                    "alias": "Study lighting",
                    "triggers": [{"trigger": "time", "at": "18:00:00"}],
                    "conditions": [],
                    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.study"}}],
                },
            },
            "Study lighting",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_automation",
            {
                "identifier": "automation.study_lighting",
                "config": {
                    "alias": "Study lighting",
                    "triggers": [{"trigger": "time", "at": "18:00:00"}],
                    "conditions": [],
                    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.study"}}],
                },
                "wait": True,
            },
            {
                "success": True,
                "action": "update",
                "entity_id": "automation.study_lighting",
                "warnings": ["Configuration saved; the automation is not yet queryable."],
            },
            "not yet queryable",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_automation",
            {"identifier": "automation.study_lighting", "wait": True},
            {
                "success": True,
                "action": "delete",
                "automation_id": "automation.study_lighting",
                "warnings": ["Removal has not yet been observed."],
            },
            "Removal has not yet been observed",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_scene",
            {"scene_id": "scene.reading"},
            {
                "success": True,
                "action": "get",
                "scene_id": "scene.reading",
                "config": {
                    "name": "Reading scene",
                    "entities": {"light.reading_lamp": {"state": "on", "brightness": 142}},
                },
            },
            "Reading scene",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_scene",
            {
                "scene_id": "scene.reading",
                "config": {
                    "name": "Reading scene",
                    "entities": {"light.reading_lamp": {"state": "on", "brightness": 142}},
                },
                "wait": False,
            },
            {"success": True, "action": "update", "scene_id": "scene.reading"},
            "scene.reading",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_scene",
            {"scene_id": "scene.reading", "wait": True},
            {"success": True, "action": "delete", "scene_id": "scene.reading"},
            "Delete",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_script",
            {"script_id": "script.read_a_book"},
            {
                "success": True,
                "action": "get",
                "script_id": "script.read_a_book",
                "config": {
                    "alias": "Read a book",
                    "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.reading_lamp"}}],
                },
            },
            "Read a book",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_script",
            {
                "script_id": "read_a_book",
                "config": {
                    "alias": "Read a book",
                    "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.reading_lamp"}}],
                },
                "wait": True,
            },
            {"success": True, "action": "upsert", "script_id": "script.read_a_book", "entity_id": "script.read_a_book"},
            "script.read_a_book",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_script",
            {"script_id": "script.read_a_book", "wait": True},
            {"success": True, "action": "delete", "script_id": "script.read_a_book"},
            "Delete",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_dashboard",
            {"url_path": "study-panel", "entity_id": "light.study", "mode": "search"},
            {
                "success": True,
                "action": "find_card",
                "url_path": "study-panel",
                "matches": [{"type": "tile", "heading": "Study lamp", "entity_id": "light.study"}],
                "match_count": 1,
            },
            "light.study",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_dashboard",
            {
                "url_path": "study-panel",
                "title": "Study panel",
                "require_admin": True,
                "show_in_sidebar": False,
                "config": {"views": [{"title": "Study", "cards": []}]},
            },
            {
                "success": True,
                "message": "Dashboard study-panel updated successfully",
                "action": "update",
                "url_path": "study-panel",
                "dashboard_created": False,
                "config_updated": True,
                "metadata_updated": True,
            },
            "updated successfully",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_delete_dashboard",
            {"url_path": "study-panel"},
            {
                "success": True,
                "message": "Dashboard already deleted or does not exist",
                "action": "delete",
                "url_path": "study-panel",
            },
            "already deleted",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_list_dashboard_resources",
            {"limit": 10},
            {
                "success": True,
                "resources": [{"url": "/local/study-card.js", "type": "module", "id": "resource-study"}],
                "count": 1,
            },
            "/local/study-card.js",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_dashboard_resource",
            {"url": "/local/study-card.js", "resource_type": "module"},
            {"success": True, "action": "create", "resource_id": "resource-study", "url": "/local/study-card.js"},
            "/local/study-card.js",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_delete_dashboard_resource",
            {"resource_id": "resource-study"},
            {
                "success": True,
                "message": "Resource deleted successfully",
                "action": "delete",
                "resource_id": "resource-study",
            },
            "deleted successfully",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_category",
            {"scope": "automation"},
            {
                "success": True,
                "scope": "automation",
                "count": 1,
                "categories": [{"category_id": "lighting", "name": "Lighting", "icon": "mdi:lightbulb"}],
            },
            "Lighting",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_category",
            {"scope": "automation", "name": "Study routines", "icon": "mdi:desk"},
            {
                "success": True,
                "scope": "automation",
                "category_id": "study-routines",
                "category_data": {"name": "Study routines", "icon": "mdi:desk"},
            },
            "Study routines",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_category",
            {"scope": "automation", "category_id": "lighting"},
            {
                "success": True,
                "message": "Successfully deleted category",
                "scope": "automation",
                "category_id": "lighting",
            },
            "deleted category",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_label",
            {"label_id": "maintenance"},
            {
                "success": True,
                "label_id": "maintenance",
                "label": {"name": "Maintenance", "color": "blue", "icon": "mdi:tools"},
            },
            "Maintenance",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_label",
            {"name": "Maintenance", "color": "blue"},
            {"success": True, "label_id": "maintenance", "label_data": {"name": "Maintenance", "color": "blue"}},
            "Maintenance",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_label",
            {"label_id": "maintenance"},
            {"success": True, "message": "Successfully deleted label", "label_id": "maintenance"},
            "deleted label",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_list_groups",
            {"limit": 5},
            {"success": True, "groups": [{"entity_id": "group.study_lights", "name": "Study lights", "state": "off"}]},
            "Study lights",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_group",
            {
                "object_id": "study_lights",
                "name": "Study lights",
                "add_entities": ["light.study"],
                "remove_entities": ["light.old_lamp"],
            },
            {
                "success": True,
                "message": "Successfully updated group",
                "entity_id": "group.study_lights",
                "object_id": "study_lights",
                "updated_fields": ["add_entities", "remove_entities"],
            },
            "updated group",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_group",
            {"object_id": "study_lights", "wait": True},
            {
                "success": True,
                "message": "Successfully removed group",
                "entity_id": "group.study_lights",
                "object_id": "study_lights",
            },
            "removed group",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_list_helpers",
            {"helper_type": "input_boolean", "limit": 5},
            {
                "success": True,
                "helper_type": "input_boolean",
                "count": 1,
                "helpers": [{"name": "Desk occupied", "entity_id": "input_boolean.desk_occupied"}],
            },
            "Desk occupied",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_helper",
            {"helper_type": "input_boolean", "action": "create", "name": "Desk occupied", "initial": False},
            {
                "success": True,
                "message": "Helper created",
                "action": "create",
                "helper_type": "input_boolean",
                "entity_id": "input_boolean.desk_occupied",
            },
            "Helper created",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_helpers_integrations",
            {"target": "input_boolean.desk_occupied", "helper_type": "input_boolean", "confirm": True},
            {
                "success": True,
                "message": "Helper input_boolean.desk_occupied removed",
                "entity_id": "input_boolean.desk_occupied",
            },
            "desk_occupied removed",
        ),
        ActionExample(
            "home_assistant",
            "ha_set_area_or_floor",
            {"kind": "area", "name": "Study", "floor_id": "ground"},
            {
                "success": True,
                "message": "Area Study created",
                "kind": "area",
                "area": {"name": "Study", "floor_id": "ground"},
            },
            "Study created",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_area_or_floor",
            {"kind": "area", "id": "study"},
            {"success": True, "message": "Area study removed", "kind": "area", "area_id": "study"},
            "study removed",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_zone",
            {"zone_id": "zone.station"},
            {"success": True, "zone": {"entity_id": "zone.station", "name": "Station", "radius": 100}},
            "Station",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_set_zone",
            {"zone_id": "zone.station", "name": "Station", "radius": 120},
            {"success": True, "zone_data": {"name": "Station", "radius": 120}, "updated_fields": ["radius"]},
            "Station",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_zone",
            {"zone_id": "zone.station"},
            {"success": True, "message": "Successfully deleted zone", "zone_id": "zone.station"},
            "deleted zone",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_set_device",
            {"device_id": "device-study-sensor", "name": "Study sensor", "area_id": "study"},
            {"success": True, "device_id": "device-study-sensor", "name": "Study sensor", "area_id": "study"},
            "Study sensor",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_device",
            {"device_id": "device-study-sensor"},
            {
                "success": True,
                "message": "Removed Study sensor",
                "device_id": "device-study-sensor",
                "device_name": "Study sensor",
                "config_entries_removed": 1,
            },
            "Removed Study sensor",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_set_entity",
            {"entity_id": "light.study", "name": "Study lamp", "area_id": "study", "enabled": True},
            {"success": True, "entity_id": "light.study", "name": "Study lamp", "updated_fields": ["name", "area_id"]},
            "Study lamp",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_entity",
            {"entity_id": "light.study"},
            {"success": True, "entity_id": "light.study"},
            "Entity removed",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_set_integration",
            {"entry_id": "weather-source", "enabled": False},
            {
                "success": True,
                "message": "Integration disabled successfully",
                "entry_id": "weather-source",
                "require_restart": False,
                "note": "Integration has been unloaded.",
            },
            "disabled successfully",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_history",
            {"entity_ids": ["sensor.study_temperature"], "start_time": "2026-10-12T00:00:00Z"},
            {
                "success": True,
                "source": "history",
                "entities": [
                    {
                        "entity_id": "sensor.study_temperature",
                        "states": [{"state": "21.5", "last_changed": "2026-10-12T07:05:00Z"}],
                    }
                ],
            },
            "21.5",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_logs",
            {"source": "error_log", "level": "warning", "search": "retry", "limit": 10},
            {
                "success": True,
                "source": "error_log",
                "log": "2026-10-12T07:05:00Z WARNING Sensor refresh will retry",
                "returned_lines": 1,
                "total_lines": 1,
            },
            "Sensor refresh will retry",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_automation_traces",
            {"automation_id": "automation.evening_lighting", "limit": 2},
            {
                "success": True,
                "automation_id": "automation.evening_lighting",
                "trace_count": 1,
                "total_available": 1,
                "has_more": False,
                "traces": [
                    {
                        "run_id": "trace-afac",
                        "state": "stopped",
                        "timestamp": {"start": "2026-10-12T07:05:00Z"},
                        "trigger": "Time trigger",
                    }
                ],
            },
            "Time trigger",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_camera_image",
            {"entity_id": "camera.entry_preview", "width": 64},
            None,
            "Camera snapshot",
            wire_result={
                "content": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": (
                            "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAkCAAAAAAcZ2NOAAAAt0lEQVR4nIXBAUYDAACG0e9ASTKZ"
                            "ZCZJJpPMzCTJZJKkI2SSJJMkM5OZSZJJkiTJTMfqBt//HnPJmWM+6TgWknPHYnLhKCSXjqXkylFM"
                            "rh3LSdexktw4Ssmto5zcOVaTe8da0nOsJ33HRjJwVJIHx2YydFSTkWMrGTu2k0dHLXly1JNnRyN5"
                            "cTSTiWMneXXsJm+OveTdsZ98OFrJp+Mg+XK0k2/HYfLjOEp+HcfJ1HGSzBynyZ/7B5uCPiWcEdBM"
                            "AAAAAElFTkSuQmCC"
                        ),
                    }
                ]
            },
        ),
        ActionExample(
            "home_assistant",
            "ha_get_system_health",
            {"include": "config_check"},
            {
                "success": True,
                "component_count": 1,
                "health_info": {"homeassistant": {"version": "2026.10.1"}},
                "config_check": {"result": "valid"},
            },
            "2026.10.1",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_operation_status",
            {"operation_id": "operation-review", "timeout_seconds": 0},
            {
                "operation_id": "operation-review",
                "status": "pending",
                "entity_id": "light.study",
                "action": "off",
                "time_remaining_ms": 800,
                "expected_state": "off",
            },
            "Pending",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_entity_exposure",
            {"entity_id": "light.study"},
            {
                "success": True,
                "entity_id": "light.study",
                "exposed_to": {"conversation": True, "google_assistant": False},
                "has_custom_settings": True,
                "is_exposed_anywhere": True,
            },
            "conversation",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_get_integration",
            {"query": "weather", "limit": 10},
            {
                "success": True,
                "entries": [
                    {"entry_id": "sample-weather", "title": "Weather station", "domain": "weather", "state": "loaded"}
                ],
                "state_summary": {"loaded": 1},
            },
            "Weather station",
        ),
        ActionExample(
            "home_assistant",
            "ha_get_todo",
            {"entity_id": "todo.maintenance", "status": "needs_action"},
            {
                "success": True,
                "entity_id": "todo.maintenance",
                "count": 1,
                "status_filter": "needs_action",
                "items": [
                    {"uid": "item-1", "summary": "Replace filter", "status": "needs_action", "due": "2026-10-15"}
                ],
            },
            "Replace filter",
        ),
        ActionExample(
            "home_assistant",
            "ha_set_todo_item",
            {"entity_id": "todo.maintenance", "item": "Replace filter", "status": "completed"},
            {
                "success": True,
                "entity_id": "todo.maintenance",
                "item": "Replace filter",
                "updates": {"status": "completed"},
                "message": "Successfully updated 'Replace filter' in todo.maintenance",
            },
            "completed",
        ),
        ActionExample(
            "home_assistant",
            "ha_remove_todo_item",
            {"entity_id": "todo.maintenance", "item": "Replace filter"},
            {"success": True, "entity_id": "todo.maintenance", "item": "Replace filter"},
            "Item removed",
            compact_card_expands=False,
        ),
        ActionExample(
            "home_assistant",
            "ha_config_get_calendar_events",
            {"entity_id": "calendar.maintenance", "start": "2026-10-12", "end": "2026-10-14"},
            {
                "success": True,
                "entity_id": "calendar.maintenance",
                "events": [
                    {"summary": "Service appointment", "start": {"date": "2026-10-13"}, "end": {"date": "2026-10-14"}}
                ],
                "count": 1,
                "total_available": 1,
            },
            "Service appointment",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_set_calendar_event",
            {
                "entity_id": "calendar.maintenance",
                "summary": "Replace filter",
                "start": "2026-10-13",
                "end": "2026-10-14",
                "rrule": "FREQ=MONTHLY;COUNT=3",
            },
            {
                "success": True,
                "entity_id": "calendar.maintenance",
                "event": {
                    "summary": "Replace filter",
                    "start": "2026-10-13",
                    "end": "2026-10-14",
                    "rrule": "FREQ=MONTHLY;COUNT=3",
                },
            },
            "Event created",
        ),
        ActionExample(
            "home_assistant",
            "ha_config_remove_calendar_event",
            {
                "entity_id": "calendar.maintenance",
                "uid": "event-maintenance",
                "recurrence_id": "2026-10-13",
                "recurrence_range": "THIS_AND_FUTURE",
            },
            {
                "success": True,
                "entity_id": "calendar.maintenance",
                "uid": "event-maintenance",
                "recurrence_id": "2026-10-13",
                "recurrence_range": "THIS_AND_FUTURE",
            },
            "Deletion confirmed",
        ),
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
                "services": {
                    "fan.set_percentage": {"name": "Set fan speed", "description": "Set the percentage speed."}
                },
            },
            "Set fan speed",
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
