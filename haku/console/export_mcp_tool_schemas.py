"""Export the console's advertised in-process MCP input schemas for frontend validation.

The MCP ``tools/list`` response is the source of truth for JSON-Schema-expressible
argument structure. This exporter builds the same FastMCP servers used by the console
and reflects them through an in-memory ``Client`` so FastMCP's normal protocol
middleware (including schema dereferencing) runs before the schemas reach the generated
frontend catalog. Execution-only Python validators can impose stricter cross-field rules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from fastmcp import Client, FastMCP

from haku.console.in_process_servers import InProcessServerDependencies, build_in_process_servers

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
    """Build every same-repository in-process server without live credentials."""

    inert = _InertCollaborator()
    # `Any` is intentional at this reflection-only boundary: no collaborator may be
    # touched until a tool executes, and `_InertCollaborator` makes that invariant fail
    # loudly if FastMCP ever changes its registration behavior.
    dependency: Any = inert
    servers = build_in_process_servers(
        InProcessServerDependencies(gmail=dependency, calendar=dependency, routine_launcher=dependency)
    )
    return dict(sorted(servers.items()))


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
        async with Client(server) as client:
            tools = sorted(await client.list_tools(), key=lambda tool: tool.name)

        tool_properties: dict[str, Any] = {}
        for tool in tools:
            input_schema = tool.inputSchema
            if not isinstance(input_schema, dict):
                raise ValueError(f"{server_id}.{tool.name} published a non-object input schema")
            _validate_frontend_schema(input_schema, f"$.properties.{server_id}.properties.{tool.name}")
            tool_properties[tool.name] = input_schema

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
