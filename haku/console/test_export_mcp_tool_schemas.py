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


async def test_exports_every_server_and_tool() -> None:
    schema = await build_mcp_tool_arguments_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "McpToolArguments"
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["gmail", "google_calendar", "grocy-sf", "haku_routine"]
    assert schema["required"] == ["gmail", "google_calendar", "grocy-sf", "haku_routine"]
    assert list(schema["properties"]["gmail"]["properties"]) == [
        "drafts_create",
        "labels_create",
        "labels_delete",
        "labels_get",
        "labels_list",
        "labels_patch",
        "messages_get",
        "threads_get",
        "threads_list",
        "threads_modify_labels",
    ]
    assert list(schema["properties"]["google_calendar"]["properties"]) == ["create_calendar_event"]
    assert list(schema["properties"]["haku_routine"]["properties"]) == ["launch_routine"]
    # grocy-sf is reflected only for the batch tools the console renders previews for.
    assert list(schema["properties"]["grocy-sf"]["properties"]) == [
        "products_create",
        "products_edit",
        "products_list",
        "quantity_units_list",
        "shopping_list_get",
        "shopping_list_item_edit",
        "shopping_list_items_add",
        "shopping_list_items_remove",
        "stock_add",
        "stock_consume",
        "stock_entry_edit",
        "stock_get",
    ]
    for server in schema["properties"].values():
        assert server["additionalProperties"] is False
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
    calendar = schema["properties"]["google_calendar"]["properties"]["create_calendar_event"]

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
        }
    )


async def test_exports_result_catalog_for_in_process_servers() -> None:
    schema = await build_mcp_tool_results_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "McpToolResults"
    assert schema["additionalProperties"] is False
    # grocy-sf is excluded — its result schemas stay hand-authored in the frontend.
    assert list(schema["properties"]) == ["gmail", "google_calendar", "haku_routine"]
    assert schema["required"] == ["gmail", "google_calendar", "haku_routine"]

    gmail = schema["properties"]["gmail"]["properties"]
    # A `-> None` return (gmail.labels_delete) has only a null wrapped result, so it is omitted —
    # the result tool set is a subset of the argument tool set.
    assert "labels_delete" not in gmail
    assert "drafts_create" in gmail
    assert "threads_modify_labels" in gmail
    # `id` is the one required field of a Draft resource.
    assert gmail["drafts_create"].get("required") == ["id"]
    assert list(schema["properties"]["google_calendar"]["properties"]) == ["create_calendar_event"]
    assert list(schema["properties"]["haku_routine"]["properties"]) == ["launch_routine"]
    for server in schema["properties"].values():
        assert server["additionalProperties"] is False

    Draft202012Validator.check_schema(schema)


async def test_result_schemas_validate_and_terminate_recursion() -> None:
    """Result schemas accept representative payloads, and a cyclic nested model (gmail's
    `MessagePart.parts: list[MessagePart]`) terminates as a permissive object rather than an
    infinite `$ref` — no surviving references reach the frontend."""
    schema = await build_mcp_tool_results_schema()
    gmail = schema["properties"]["gmail"]["properties"]
    calendar = schema["properties"]["google_calendar"]["properties"]["create_calendar_event"]

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

    # CreateCalendarEventResult's `id`/`htmlLink` are input-only aliases, so the wire shape is the
    # Python field names exactly.
    Draft202012Validator(calendar).validate({"event_id": "evt-1", "html_link": "https://cal/evt-1"})


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
