from __future__ import annotations

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator
from mcp_types import Tool

from haku.console.mcp import export_tool_schemas
from haku.console.mcp.export_tool_schemas import _validate_frontend_schema, build_mcp_tool_schemas

# The types-jsonschema stubs import referencing, so mypy needs the dist wherever
# jsonschema is imported; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//referencing


async def test_builds_both_catalogs_from_one_server_reflection(monkeypatch: pytest.MonkeyPatch) -> None:
    server = export_tool_schemas.FastMCP("catalog-test")
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    tools = [
        Tool(
            name="search",
            input_schema=input_schema,
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "array", "items": {"type": "string"}}},
                "required": ["result"],
                "x-fastmcp-wrap-result": True,
            },
        ),
        Tool(name="clear", input_schema=input_schema, output_schema={"type": "null"}),
        Tool(name="legacy", input_schema=input_schema, output_schema=None),
    ]
    server_builds = 0
    reflections = 0

    def build_test_servers() -> dict[str, export_tool_schemas.FastMCP]:
        nonlocal server_builds
        server_builds += 1
        return {"test-server": server}

    class CountingClient:
        async def __aenter__(self) -> CountingClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_tools(self) -> list[Tool]:
            nonlocal reflections
            reflections += 1
            return tools

    def create_client(_server: export_tool_schemas.FastMCP) -> CountingClient:
        assert _server is server
        return CountingClient()

    monkeypatch.setattr(export_tool_schemas, "build_schema_servers", build_test_servers)
    monkeypatch.setattr(export_tool_schemas, "Client", create_client)

    catalogs = await build_mcp_tool_schemas()

    assert server_builds == 1
    assert reflections == 1
    assert set(catalogs.arguments["properties"]["test-server"]["properties"]) == {"search", "clear", "legacy"}
    assert set(catalogs.results["properties"]["test-server"]["properties"]) == {"search"}
    assert catalogs.results["properties"]["test-server"]["properties"]["search"] == {
        "type": "array",
        "items": {"type": "string"},
    }


async def test_every_exported_tool_schema_is_a_closed_valid_object() -> None:
    schema = (await build_mcp_tool_schemas()).arguments

    for server in schema["properties"].values():
        for tool_schema in server["properties"].values():
            assert tool_schema["additionalProperties"] is False

    Draft202012Validator.check_schema(schema)


async def test_grocy_schemas_are_inlined_and_validate() -> None:
    """grocy-sf's nested-model args come through fully inlined (no surviving $ref/$defs) and accept a
    representative payload."""
    schema = (await build_mcp_tool_schemas()).arguments
    grocy = schema["properties"]["grocy-sf"]["properties"]

    Draft202012Validator(grocy["stock_add"]).validate(
        {"items": [{"product": "Rolled oats", "amount": 2, "qu": "pack", "location": "Pantry"}]}
    )


async def test_nullable_fastmcp_arguments_remain_nullable() -> None:
    schema = (await build_mcp_tool_schemas()).arguments
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
    # update_event is a partial patch: every field but event_id is nullable/omittable.
    Draft202012Validator(schema["properties"]["google_calendar"]["properties"]["update_event"]).validate(
        {"event_id": "series1", "summary": "Renamed event"}
    )


async def test_result_catalog_is_a_subset_of_the_argument_catalog_without_none_returns() -> None:
    catalogs = await build_mcp_tool_schemas()
    arguments = catalogs.arguments
    schema = catalogs.results

    for server_id, server in arguments["properties"].items():
        assert set(schema["properties"][server_id]["properties"]) <= set(server["properties"])
    # A `-> None` return has only a null wrapped result, so it is omitted from the result catalog.
    assert "labels_delete" not in schema["properties"]["gmail"]["properties"]
    assert "delete_event" not in schema["properties"]["google_calendar"]["properties"]

    Draft202012Validator.check_schema(schema)


async def test_an_untyped_dict_result_carries_no_generated_properties() -> None:
    """shopping_list_get returns an untyped dict → an empty object schema with no properties, so it
    cannot drive a generated result widget and stays hand-authored."""
    schema = (await build_mcp_tool_schemas()).results

    assert schema["properties"]["grocy-sf"]["properties"]["shopping_list_get"].get("properties") in (None, {})


async def test_result_schemas_validate_and_terminate_recursion() -> None:
    """Result schemas accept representative payloads, and a cyclic nested model (gmail's
    `MessagePart.parts: list[MessagePart]`) terminates as a permissive object rather than an
    infinite `$ref` — no surviving references reach the frontend."""
    schema = (await build_mcp_tool_schemas()).results
    gmail = schema["properties"]["gmail"]["properties"]

    # Minimal Draft (message absent) and a full one (camelCase wire aliases from gmail_api's
    # to_camel) both validate; the nested message's recursive `parts` items is a permissive object.
    Draft202012Validator(gmail["drafts_create"]).validate({"id": "r-123"})
    Draft202012Validator(gmail["drafts_create"]).validate({"id": "r-123", "message": {"id": "m1", "threadId": "t42"}})
    message = gmail["drafts_create"]["properties"]["message"]["anyOf"][0]["properties"]
    parts_items = message["payload"]["anyOf"][0]["properties"]["parts"]["anyOf"][0]["items"]
    assert parts_items == {"type": "object"}


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        pytest.param({"$ref": {}}, "unresolved schema reference", id="surviving-reference"),
        pytest.param({"type": "string", "if": {}}, "frontend-unreviewed JSON Schema keyword", id="unreviewed-keyword"),
    ],
)
def test_rejects_schemas_the_frontend_adapter_was_not_reviewed_against(schema: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message) as error:
        _validate_frontend_schema(schema, "$.tool")
    assert "$.tool" in str(error.value)


if __name__ == "__main__":
    pytest_bazel.main()
