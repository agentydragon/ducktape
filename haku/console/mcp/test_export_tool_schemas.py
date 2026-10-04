from __future__ import annotations

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator

from haku.console.mcp.export_tool_schemas import (
    _validate_frontend_schema,
    build_mcp_tool_arguments_schema,
    build_mcp_tool_results_schema,
)

# The types-jsonschema stubs import referencing, so mypy needs the dist wherever
# jsonschema is imported; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//referencing


async def test_every_exported_tool_schema_is_a_closed_valid_object() -> None:
    schema = await build_mcp_tool_arguments_schema()

    for server in schema["properties"].values():
        for tool_schema in server["properties"].values():
            assert tool_schema["additionalProperties"] is False

    Draft202012Validator.check_schema(schema)


async def test_grocy_schemas_are_inlined_and_validate() -> None:
    """grocy-sf's nested-model args come through fully inlined (no surviving $ref/$defs) and
    accept representative payloads — the date `format` and set `uniqueItems` survive."""
    schema = await build_mcp_tool_arguments_schema()
    grocy = schema["properties"]["grocy-sf"]["properties"]

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
    # update_event is a partial patch: every field but event_id is nullable/omittable.
    Draft202012Validator(schema["properties"]["google_calendar"]["properties"]["update_event"]).validate(
        {"event_id": "series1", "summary": "Renamed event"}
    )
    Draft202012Validator(schema["properties"]["google_calendar"]["properties"]["delete_event"]).validate(
        {"event_id": "series1"}
    )


async def test_result_catalog_is_a_subset_of_the_argument_catalog_without_none_returns() -> None:
    arguments = await build_mcp_tool_arguments_schema()
    schema = await build_mcp_tool_results_schema()

    for server_id, server in arguments["properties"].items():
        assert set(schema["properties"][server_id]["properties"]) <= set(server["properties"])
    # A `-> None` return has only a null wrapped result, so it is omitted from the result catalog.
    assert "labels_delete" not in schema["properties"]["gmail"]["properties"]
    assert "delete_event" not in schema["properties"]["google_calendar"]["properties"]

    Draft202012Validator.check_schema(schema)


async def test_grocy_result_schemas_validate() -> None:
    """grocy-sf batch-tool result schemas accept representative payloads and stay reference-free.
    shopping_list_get returns an untyped dict, so its schema carries no properties — the boundary
    that keeps that one widget hand-authored."""
    schema = await build_mcp_tool_results_schema()
    grocy = schema["properties"]["grocy-sf"]["properties"]

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
    Draft202012Validator(calendar["update_event"]).validate(event)
    Draft202012Validator(calendar["list_events"]).validate({"events": [event], "next_page_token": "next"})
    Draft202012Validator(calendar["list_event_instances"]).validate({"events": [event]})


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
