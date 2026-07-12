from __future__ import annotations

import json

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator

from haku.console.export_mcp_tool_schemas import (
    _validate_frontend_schema,
    build_mcp_tool_arguments_schema,
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
        "shopping_list_get",
        "shopping_list_item_edit",
        "shopping_list_items_add",
        "stock_add",
        "stock_consume",
        "stock_entry_edit",
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
