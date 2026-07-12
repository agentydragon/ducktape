"""Export the console's advertised MCP input schemas for frontend validation.

The MCP ``tools/list`` response is the source of truth for JSON-Schema-expressible
argument structure. This exporter builds the FastMCP servers whose tools the console
renders and reflects them through an in-memory ``Client`` so FastMCP's normal protocol
middleware runs before the schemas reach the generated frontend catalog. Execution-only
Python validators can impose stricter cross-field rules.

Beyond the console's own in-process servers (gmail, google_calendar, haku_routine) this
also reflects the **remote** ``grocy-sf`` server's custom batch tools. grocy-sf runs
elsewhere, but its batch tools are ordinary Python (``grocy_mcp.batch_tools``): building a
batch-tools-only FastMCP registers them without an OpenAPI spec or a Grocy connection, so
their argument schemas are generated from ``grocy_mcp``'s Pydantic models rather than
hand-authored in the frontend. Only the tools the console previews are emitted for it
(``_SERVER_TOOL_ALLOWLIST``); nested-model ``$ref``s are inlined first (``_dereference``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

import httpx
from fastmcp import Client, FastMCP

from grocy_mcp.batch_tools import build_batch_tools_mcp
from grocy_mcp.mcp_types import ServerSettings
from haku.console.in_process_servers import InProcessServerDependencies, build_in_process_servers

GROCY_SF_SERVER_ID = "grocy-sf"

# grocy-sf is reflected only for the batch tools the console renders previews for; the rest of
# its surface isn't used by the frontend and shouldn't gate schema generation.
_SERVER_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    GROCY_SF_SERVER_ID: frozenset(
        {
            "stock_add",
            "stock_consume",
            "products_create",
            "products_edit",
            "shopping_list_get",
            "shopping_list_items_add",
            "shopping_list_item_edit",
        }
    )
}

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# Keep this deliberately narrower than JSON Schema itself.  These are the schema
# constructs the frontend's z.fromJSONSchema adapter is reviewed and tested against.
# A newly-published FastMCP construct should fail generation until the adapter gains a
# test for it, rather than silently weakening a trusted approval preview.
_FRONTEND_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        # `format` (e.g. "date") and `uniqueItems` (from Python `set` fields) appear in the grocy
        # batch tools' schemas; z.fromJSONSchema honors both. See test_export_mcp_tool_schemas.
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset({"properties"})
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})
_SCHEMA_VALUE_KEYWORDS = frozenset({"additionalProperties", "items"})
_FORBIDDEN_REFERENCE_KEYWORDS = frozenset({"$defs", "$ref", "definitions"})


class _InertCollaborator:
    """A tool dependency that proves schema reflection cannot perform real work."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"MCP schema export unexpectedly accessed collaborator attribute {name!r}")


def build_schema_servers() -> dict[str, FastMCP]:
    """Build every server whose tools the console renders, without live credentials.

    The console's own in-process servers plus a batch-tools-only grocy-sf built for schema
    reflection (its httpx client is inert — registration never calls it).
    """

    inert = _InertCollaborator()
    # `Any` is intentional at this reflection-only boundary: no collaborator may be
    # touched until a tool executes, and `_InertCollaborator` makes that invariant fail
    # loudly if FastMCP ever changes its registration behavior.
    dependency: Any = inert
    servers = build_in_process_servers(
        InProcessServerDependencies(gmail=dependency, calendar=dependency, routine_launcher=dependency)
    )
    servers[GROCY_SF_SERVER_ID] = build_batch_tools_mcp(
        ServerSettings(grocy_url="https://grocy.invalid"), client=cast(httpx.AsyncClient, inert)
    )
    return dict(sorted(servers.items()))


def _dereference(schema: Any, defs: Mapping[str, Any]) -> Any:
    """Inline ``$ref`` targets and drop the ``$defs``/``definitions`` blocks that held them.

    FastMCP publishes nested Pydantic models (e.g. ``list[AddItem]``) as ``$ref``s into a
    ``$defs`` table; the frontend adapter needs them fully inlined. The grocy models are acyclic,
    so a plain recursive resolve terminates. Sibling keywords alongside a ``$ref`` (a local
    ``description``, say) are merged over the resolved target.
    """
    if isinstance(schema, Mapping):
        if "$ref" in schema:
            name = str(schema["$ref"]).rsplit("/", 1)[-1]
            if name not in defs:
                raise ValueError(f"unresolvable $ref {schema['$ref']!r}")
            target = _dereference(defs[name], defs)
            siblings = {k: _dereference(v, defs) for k, v in schema.items() if k != "$ref"}
            return {**target, **siblings} if siblings else target
        return {k: _dereference(v, defs) for k, v in schema.items() if k not in ("$defs", "definitions")}
    if isinstance(schema, list):
        return [_dereference(item, defs) for item in schema]
    return schema


def _inline_schema_refs(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Fully inline a tool input schema's internal references (no-op when it has none)."""
    defs = {**schema.get("definitions", {}), **schema.get("$defs", {})}
    inlined = _dereference(dict(schema), defs)
    assert isinstance(inlined, dict)  # a tool input schema is always an object
    return inlined


def _validate_frontend_schema(schema: object, path: str) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} must be a JSON Schema object or boolean")

    keys = set(schema)
    forbidden = keys & _FORBIDDEN_REFERENCE_KEYWORDS
    if forbidden:
        joined = ", ".join(sorted(forbidden))
        raise ValueError(f"{path} contains unresolved schema reference keyword(s): {joined}")
    unsupported = keys - _FRONTEND_SCHEMA_KEYWORDS
    if unsupported:
        joined = ", ".join(sorted(str(key) for key in unsupported))
        raise ValueError(f"{path} contains frontend-unreviewed JSON Schema keyword(s): {joined}")

    for keyword in _SCHEMA_MAP_KEYWORDS:
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, Mapping):
            raise ValueError(f"{path}.{keyword} must be an object")
        for name, child in children.items():
            _validate_frontend_schema(child, f"{path}.{keyword}.{name}")

    for keyword in _SCHEMA_ARRAY_KEYWORDS:
        children = schema.get(keyword)
        if children is None:
            continue
        if not isinstance(children, list):
            raise ValueError(f"{path}.{keyword} must be an array")
        for index, child in enumerate(children):
            _validate_frontend_schema(child, f"{path}.{keyword}[{index}]")

    for keyword in _SCHEMA_VALUE_KEYWORDS:
        child = schema.get(keyword)
        if child is not None and isinstance(child, (Mapping, bool)):
            _validate_frontend_schema(child, f"{path}.{keyword}")


async def build_mcp_tool_arguments_schema() -> dict[str, Any]:
    """Return one deterministic JSON Schema catalog keyed by server then tool."""

    server_properties: dict[str, Any] = {}
    for server_id, server in build_schema_servers().items():
        allowlist = _SERVER_TOOL_ALLOWLIST.get(server_id)
        async with Client(server) as client:
            tools = sorted(await client.list_tools(), key=lambda tool: tool.name)

        tool_properties: dict[str, Any] = {}
        for tool in tools:
            if allowlist is not None and tool.name not in allowlist:
                continue
            input_schema = tool.inputSchema
            if not isinstance(input_schema, dict):
                raise ValueError(f"{server_id}.{tool.name} published a non-object input schema")
            input_schema = _inline_schema_refs(input_schema)
            _validate_frontend_schema(input_schema, f"$.properties.{server_id}.properties.{tool.name}")
            tool_properties[tool.name] = input_schema

        if allowlist is not None and set(tool_properties) != set(allowlist):
            missing = ", ".join(sorted(allowlist - set(tool_properties)))
            raise ValueError(f"{server_id} is missing allowlisted preview tools: {missing}")

        server_properties[server_id] = {
            "type": "object",
            "additionalProperties": False,
            "properties": tool_properties,
            "required": list(tool_properties),
        }

    return {
        "$schema": _DRAFT_2020_12,
        "title": "McpToolArguments",
        "type": "object",
        "additionalProperties": False,
        "properties": server_properties,
        "required": list(server_properties),
    }


async def export_mcp_tool_schemas_json() -> str:
    return json.dumps(await build_mcp_tool_arguments_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    print(asyncio.run(export_mcp_tool_schemas_json()), end="")


if __name__ == "__main__":
    main()
