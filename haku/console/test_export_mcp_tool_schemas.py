from __future__ import annotations

import json

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator

from haku.console.export_mcp_tool_schemas import (
    _validate_frontend_schema,
    build_mcp_tool_arguments_schema,
    build_mcp_tool_results_schema,
    export_mcp_tool_results_json,
    export_mcp_tool_schemas_json,
)

_EXPECTED_TOOLS = {
    "gmail": (
        "drafts_create",
        "drafts_delete",
        "drafts_get",
        "drafts_list",
        "drafts_update",
        "filters_create",
        "filters_delete",
        "filters_get",
        "filters_list",
        "labels_create",
        "labels_delete",
        "labels_get",
        "labels_list",
        "labels_patch",
        "messages_get",
        "threads_get",
        "threads_list",
        "threads_modify_labels",
    ),
    "google_calendar": ("create_event", "get_event", "list_event_instances", "list_events"),
    # grocy-sf is reflected only for the batch tools the console renders previews for.
    "grocy-sf": (
        "locations_list",
        "product_groups_list",
        "products_create",
        "products_edit",
        "products_list",
        "quantity_units_list",
        "shopping_list_get",
        "shopping_list_item_edit",
        "shopping_list_items_add",
        "shopping_list_items_remove",
        "shopping_lists_list",
        "stock_add",
        "stock_consume",
        "stock_entry_edit",
        "stock_get",
    ),
    "haku_routine": ("launch_routine",),
    "haku_sandbox": ("exec", "info", "reserve"),
    "hostexec": ("bash",),
}
_SERVER_IDS = list(_EXPECTED_TOOLS)
_RESULT_SERVER_IDS = [*_SERVER_IDS, "haku-console"]
_RESULT_TOOLS_MATCH_ARGUMENTS = ("google_calendar", "grocy-sf", "haku_routine", "haku_sandbox", "hostexec")


def _assert_catalog_shape(schema: dict[str, object], title: str, server_ids: list[str] = _SERVER_IDS) -> None:
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == title
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == server_ids
    assert schema["required"] == server_ids
    for server in properties.values():
        assert server["additionalProperties"] is False


async def test_exports_every_server_and_tool() -> None:
    schema = await build_mcp_tool_arguments_schema()

    _assert_catalog_shape(schema, "McpToolArguments")
    for server_id, expected_tools in _EXPECTED_TOOLS.items():
        assert list(schema["properties"][server_id]["properties"]) == list(expected_tools)
    for server in schema["properties"].values():
        for tool_schema in server["properties"].values():
            assert tool_schema["additionalProperties"] is False

    Draft202012Validator.check_schema(schema)


async def test_grocy_schemas_are_inlined_and_validate() -> None:
    """grocy-sf's nested-model args come through fully inlined (no surviving $ref/$defs) and
    accept representative payloads — the date `format` and set `uniqueItems` survive."""
    schema = await build_mcp_tool_arguments_schema()
    grocy = schema["properties"]["grocy-sf"]["properties"]

    assert "$defs" not in json.dumps(grocy)
    assert "$ref" not in json.dumps(grocy)

    Draft202012Validator(grocy["stock_add"]).validate(
        {"items": [{"product": "Rolled oats", "amount": 2, "qu": "pack", "location": "Pantry"}]}
    )
    Draft202012Validator(grocy["stock_entry_edit"]).validate(
        {"items": [{"entry_id": 189, "price": 9.99, "location": "Pantry", "clear_fields": ["note"]}]}
    )
    Draft202012Validator(grocy["stock_get"]).validate({"products": ["Oats"], "locations": [2]})
    Draft202012Validator(grocy["shopping_list_items_remove"]).validate({"item_ids": [3, 7]})
    Draft202012Validator(grocy["products_edit"]).validate(
        {"items": [{"product": 42, "min_stock_amount": 500, "clear_fields": ["description"]}]}
    )
    Draft202012Validator(grocy["shopping_list_item_edit"]).validate({"item_id": 7, "amount": 3, "done": True})


async def test_export_is_stable_json() -> None:
    first = await export_mcp_tool_schemas_json()
    second = await export_mcp_tool_schemas_json()

    assert first == second
    assert json.loads(first)["title"] == "McpToolArguments"
    assert first.endswith("\n")


async def test_nullable_fastmcp_arguments_remain_nullable() -> None:
    schema = await build_mcp_tool_arguments_schema()
    gmail = schema["properties"]["gmail"]["properties"]
    calendar = schema["properties"]["google_calendar"]["properties"]["create_event"]

    Draft202012Validator(gmail["threads_modify_labels"]).validate(
        {"thread_ids": ["thread-1"], "add": ["Follow up"], "remove": None}
    )
    Draft202012Validator(gmail["drafts_create"]).validate(
        {"to": ["operator@example.com"], "subject": "Subject", "body": "Body", "cc": None}
    )
    Draft202012Validator(calendar).validate(
        {
            "summary": "Event",
            "start": {"date": "2026-07-11"},
            "end": {"date": "2026-07-12"},
            "reminders": None,
            "attendees": None,
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12"],
        }
    )
    Draft202012Validator(schema["properties"]["google_calendar"]["properties"]["get_event"]).validate(
        {"event_id": "series1"}
    )


async def test_hostexec_schemas_validate() -> None:
    """hostexec's `bash` tool round-trips through both catalogs: its args (including the omittable
    `cwd`) and its `BaseExecResult`-shaped result, whose `exit` field is a `kind`-discriminated
    union (`Field(discriminator="kind")`) — proving that survives `_dereference` cleanly too."""
    args_schema = (await build_mcp_tool_arguments_schema())["properties"]["hostexec"]["properties"]["bash"]
    Draft202012Validator(args_schema).validate(
        {"host": "wyrm2", "run_as": "agentydragon", "cmd": "ls -la", "max_bytes": 1000, "timeout_ms": 5000}
    )
    Draft202012Validator(args_schema).validate(
        {"host": "wyrm2", "run_as": "root", "cmd": "true", "max_bytes": 0, "timeout_ms": 1000, "cwd": "/tmp"}
    )

    result_schema = (await build_mcp_tool_results_schema())["properties"]["hostexec"]["properties"]["bash"]
    Draft202012Validator(result_schema).validate(
        {"exit": {"kind": "exited", "exit_code": 0}, "stdout": "hi", "stderr": "", "duration_ms": 12}
    )
    Draft202012Validator(result_schema).validate(
        {
            "exit": {"kind": "killed", "signal": 9},
            "stdout": {"truncated_text": "partial", "total_bytes": 5_000},
            "stderr": "",
            "duration_ms": 30_000,
        }
    )


async def test_haku_sandbox_schemas_validate() -> None:
    args = (await build_mcp_tool_arguments_schema())["properties"]["haku_sandbox"]["properties"]
    Draft202012Validator(args["reserve"]).validate({})
    Draft202012Validator(args["info"]).validate({"handle": "hs-abc12"})
    Draft202012Validator(args["exec"]).validate(
        {"handle": "hs-abc12", "cmd": ["bash", "-lc", "echo ok"], "timeout_ms": 5000}
    )

    results = (await build_mcp_tool_results_schema())["properties"]["haku_sandbox"]["properties"]
    Draft202012Validator(results["info"]).validate(
        {
            "handle": "hs-abc12",
            "state": "expired",
            "healthy": False,
            "expires_at": "2026-07-22T20:00:00Z",
            "sandbox_name": None,
            "pod_name": None,
            "reason": "ClaimExpired",
            "message": "lease expired",
        }
    )
    Draft202012Validator(results["exec"]).validate(
        {
            "exit": {"kind": "exited", "exit_code": 0},
            "stdout": "ok",
            "stderr": "",
            "duration_ms": 4,
            "expires_at": "2026-07-22T20:00:00Z",
        }
    )


async def test_exports_result_catalog() -> None:
    schema = await build_mcp_tool_results_schema()

    _assert_catalog_shape(schema, "McpToolResults", _RESULT_SERVER_IDS)
    # grocy-sf's batch tools are reflected (typed ducktape result models), limited to the same
    # preview allowlist as its argument schemas. get_system_info is an OpenAPI tool with no batch
    # counterpart, so it is absent and its widget stays hand-authored.
    for server_id in _RESULT_TOOLS_MATCH_ARGUMENTS:
        assert list(schema["properties"][server_id]["properties"]) == list(_EXPECTED_TOOLS[server_id])

    gmail = schema["properties"]["gmail"]["properties"]
    # A `-> None` return (gmail.labels_delete) has only a null wrapped result, so it is omitted —
    # the result tool set is a subset of the argument tool set.
    assert "labels_delete" not in gmail
    assert "thread_previews" not in gmail
    assert "drafts_create" in gmail
    assert "threads_modify_labels" in gmail
    # `id` is the one required field of a Draft resource.
    assert gmail["drafts_create"].get("required") == ["id"]

    Draft202012Validator.check_schema(schema)


async def test_exports_console_native_status_result_schemas() -> None:
    schema = await build_mcp_tool_results_schema()
    tools = schema["properties"]["haku-console"]["properties"]

    assert list(tools) == ["get_mcp_server_status", "list_mcp_servers", "list_node_daemons"]

    schemas_by_title: dict[str, dict] = {}

    def collect_titled_schemas(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("title"), str):
                schemas_by_title[value["title"]] = value
            for child in value.values():
                collect_titled_schemas(child)
        elif isinstance(value, list):
            for child in value:
                collect_titled_schemas(child)

    collect_titled_schemas(tools["list_mcp_servers"])
    for title in (
        "McpOperatorAuthConnected",
        "McpOperatorAuthDegraded",
        "McpOperatorAuthUnconnected",
        "ProviderConnected",
        "ProviderDegraded",
        "ProviderUnconnected",
        "ProviderUnprovisioned",
    ):
        assert "status" in schemas_by_title[title]["required"]
    refresh_failure_schema = schemas_by_title["OAuthRefreshFailureEpisode"]
    assert {"resolution", "next_retry_at"} <= set(refresh_failure_schema["required"])
    assert "action" not in refresh_failure_schema["properties"]

    Draft202012Validator(tools["list_mcp_servers"]).validate(
        {
            "servers": [
                {
                    "server_id": "google_calendar",
                    "backend": {
                        "kind": "in_process",
                        "credential": {"kind": "operator_connection", "connection": "google_calendar"},
                    },
                    "connection": {
                        "connection": "google_calendar",
                        "display_name": "Google Calendar",
                        "provider": "google",
                        "status": "unprovisioned",
                        "detail": "OAuth client not provisioned on this console; see the console deployment README.",
                    },
                }
            ]
        }
    )


async def test_grocy_result_schemas_validate() -> None:
    """grocy-sf batch-tool result schemas accept representative payloads and stay reference-free.
    shopping_list_get returns an untyped dict, so its schema carries no properties — the boundary
    that keeps that one widget hand-authored."""
    schema = await build_mcp_tool_results_schema()
    grocy = schema["properties"]["grocy-sf"]["properties"]

    serialized = json.dumps(grocy)
    assert "$defs" not in serialized
    assert "$ref" not in serialized
    assert "x-fastmcp-wrap-result" not in serialized

    # stock_add returns a list of StockOpOk | StockOpError; `kind` defaults so it is optional.
    Draft202012Validator(grocy["stock_add"]).validate(
        [{"product_name": "Oats", "qu_name": "pack", "location_name": "Pantry"}]
    )
    Draft202012Validator(grocy["stock_add"]).validate([{"kind": "error", "error": "boom"}])
    # products_list is a union of brief/full array rows; both carry id + name.
    Draft202012Validator(grocy["products_list"]).validate([{"id": 1, "name": "Oats"}])
    Draft202012Validator(grocy["stock_get"]).validate(
        [
            {
                "product_id": 1,
                "product_name": "Oats",
                "amount": 2,
                "amount_opened": 0,
                "qu_name": "pack",
                "location_name": "Pantry",
            }
        ]
    )

    # shopping_list_get's return type is an untyped dict → an empty object schema with no
    # properties, so it cannot drive a generated result widget and stays hand-authored.
    assert grocy["shopping_list_get"].get("properties") in (None, {})


async def test_result_schemas_validate_and_terminate_recursion() -> None:
    """Result schemas accept representative payloads, and a cyclic nested model (gmail's
    `MessagePart.parts: list[MessagePart]`) terminates as a permissive object rather than an
    infinite `$ref` — no surviving references reach the frontend."""
    schema = await build_mcp_tool_results_schema()
    gmail = schema["properties"]["gmail"]["properties"]
    calendar = schema["properties"]["google_calendar"]["properties"]

    serialized = json.dumps(schema)
    assert "$defs" not in serialized
    assert "$ref" not in serialized
    assert "x-fastmcp-wrap-result" not in serialized

    # Minimal Draft (message absent) and a full one (camelCase wire aliases from gmail_api's
    # to_camel) both validate; the nested message's recursive `parts` items is a permissive object.
    Draft202012Validator(gmail["drafts_create"]).validate({"id": "r-123"})
    Draft202012Validator(gmail["drafts_create"]).validate({"id": "r-123", "message": {"id": "m1", "threadId": "t42"}})
    message = gmail["drafts_create"]["properties"]["message"]["anyOf"][0]["properties"]
    parts_items = message["payload"]["anyOf"][0]["properties"]["parts"]["anyOf"][0]["items"]
    assert parts_items == {"type": "object"}

    # Calendar API aliases are validation-only, so the focused MCP wire uses Python field names.
    event = {
        "event_id": "evt-1",
        "summary": "Standup",
        "recurrence": ["RRULE:FREQ=WEEKLY"],
        "html_link": "https://cal/evt-1",
    }
    Draft202012Validator(calendar["create_event"]).validate(event)
    Draft202012Validator(calendar["get_event"]).validate(event)
    Draft202012Validator(calendar["list_events"]).validate({"events": [event], "next_page_token": "next"})
    Draft202012Validator(calendar["list_event_instances"]).validate({"events": [event]})


async def test_results_catalog_is_stable_json() -> None:
    first = await export_mcp_tool_results_json()
    second = await export_mcp_tool_results_json()

    assert first == second
    assert json.loads(first)["title"] == "McpToolResults"
    assert first.endswith("\n")


@pytest.mark.parametrize("keyword", ["$defs", "$ref", "definitions"])
def test_rejects_surviving_schema_references(keyword: str) -> None:
    with pytest.raises(ValueError, match="unresolved schema reference"):
        _validate_frontend_schema({keyword: {}}, "$.tool")


@pytest.mark.parametrize(
    "keyword",
    [
        "contains",
        "contentMediaType",
        "dependentRequired",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    ],
)
def test_rejects_unreviewed_schema_keywords(keyword: str) -> None:
    with pytest.raises(ValueError, match="frontend-unreviewed JSON Schema keyword"):
        _validate_frontend_schema({"type": "string", keyword: {}}, "$.tool")


if __name__ == "__main__":
    pytest_bazel.main()
