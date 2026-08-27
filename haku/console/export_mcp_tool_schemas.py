"""Export the console's advertised MCP input and output schemas for frontend validation.

The MCP ``tools/list`` response is the source of truth for JSON-Schema-expressible argument
*and* result structure. This exporter builds the FastMCP servers whose tools the console renders
and reflects them through an in-memory ``Client``, so FastMCP's normal protocol middleware runs
before the schemas reach the generated frontend catalogs; execution-only Python validators can
still impose stricter cross-field rules. Two catalogs are emitted, selected by ``main()``'s
``--results`` flag: ``McpToolArguments`` and ``McpToolResults``.

Beyond the console's own in-process servers (gmail, google_calendar, haku_routine, hostexec,
kubernetes), the
result catalog includes the console-native reflection tools directly from their Python response
models, which keeps the trusted frontend's runtime validators identical to the MCP output contract
without a database-backed console application or an HTTP status endpoint.

The exporter also reflects the **remote** ``grocy-sf`` server's custom batch tools. grocy-sf runs
elsewhere, but its batch tools are ordinary Python (``grocy_mcp.batch_tools``): building a
batch-tools-only FastMCP registers them without an OpenAPI spec or a Grocy connection, so their
schemas come from ``grocy_mcp``'s Pydantic models rather than being hand-authored in the frontend.
Only the tools the console previews are emitted (``_SERVER_TOOL_ALLOWLIST``), with nested-model
``$ref``s inlined first (``_dereference``). ``grocy-sf``'s OpenAPI tools remain outside the
reflected catalog, so their result widgets stay hand-authored.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Any, cast

from fastmcp import Client, FastMCP
from pydantic import BaseModel

from grocy_mcp.batch_tools import build_batch_tools_mcp
from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from haku.console.config import HostexecConfig
from haku.console.in_process_servers import HostexecServerConfig, InProcessServerDependencies, build_in_process_servers
from haku.console.mcp_server import SERVER_NAME, McpServerConnectionStatusResponse, McpServerProbeResponse
from haku.console.node_daemons import DaemonStatusResponse
from haku.console.tools.http_grants import HttpToolsService
from haku.console.tools.kubernetes import KubernetesToolsService
from mcp_infra.request_scoped_openapi import borrowed_http_client_provider

GROCY_SF_SERVER_ID = "grocy-sf"

_CONSOLE_NATIVE_RESULT_MODELS: dict[str, type[BaseModel]] = {
    "get_mcp_server_status": McpServerProbeResponse,
    "list_mcp_servers": McpServerConnectionStatusResponse,
    "list_node_daemons": DaemonStatusResponse,
}

# grocy-sf is reflected only for the batch tools the console renders previews for; the rest of
# its surface isn't used by the frontend and shouldn't gate schema generation.
_SERVER_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    GROCY_SF_SERVER_ID: frozenset(
        {
            "stock_add",
            "stock_consume",
            "stock_entry_edit",
            "stock_get",
            "locations_list",
            "products_create",
            "products_list",
            "product_groups_list",
            "quantity_units_list",
            "products_edit",
            "shopping_list_get",
            "shopping_lists_list",
            "shopping_list_items_add",
            "shopping_list_items_remove",
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
    reflection (its Grocy client is inert — registration never calls it).
    """

    inert = _InertCollaborator()
    # `Any` is intentional at this reflection-only boundary: no collaborator may be
    # touched until a tool executes, and `_InertCollaborator` makes that invariant fail
    # loudly if FastMCP ever changes its registration behavior. gmail/google_calendar builders
    # build their own inert client from a None token; routine and hostexec need an inert
    # launcher/broker respectively, haku_index needs an inert searcher, and kubernetes/http_grants need inert
    # grant/authorization/enrollment services. hostexec's `hosts` map is empty — registration only needs the
    # tool's own schema, never a real host to route to.
    dependency: Any = inert
    servers = {
        server_id: registration.builder(None)
        for server_id, registration in build_in_process_servers(
            InProcessServerDependencies(
                routine_launcher=dependency,
                hostexec=HostexecServerConfig(config=HostexecConfig(hosts={}), token_endpoint="", broker=dependency),
                index=dependency,
                kubernetes=KubernetesToolsService(grants=dependency, authorization=dependency),
                http_grants=HttpToolsService(grants=dependency, agents=dependency),
            )
        ).items()
    }
    servers[GROCY_SF_SERVER_ID] = build_batch_tools_mcp(
        ServerSettings(grocy_url="https://grocy.invalid"),
        client_provider=borrowed_http_client_provider(cast(GrocyClient, inert)),
    )
    return dict(sorted(servers.items()))


def _dereference(schema: Any, defs: Mapping[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    """Inline ``$ref`` targets and drop the ``$defs``/``definitions`` blocks that held them.

    FastMCP publishes nested Pydantic models (e.g. ``list[AddItem]``) as ``$ref``s into a
    ``$defs`` table; the frontend adapter needs them fully inlined. Sibling keywords alongside a
    ``$ref`` (a local ``description``, say) are merged over the resolved target.

    Cyclic models (a ``$ref`` whose target eventually re-references itself — e.g. gmail's
    ``MessagePart.parts: list[MessagePart]``) terminate by substituting the target's declared
    top-level ``type`` for the back-edge: the recursive branch becomes permissive (any object of
    that type) rather than inlined forever, so a real payload still validates while the schema
    stays free of ``$ref``/``$defs``. ``seen`` is the set of ref names on the current resolution
    path, threaded immutably so parallel branches can reuse a shared type without falsely cycling.
    """
    if isinstance(schema, Mapping):
        if "$ref" in schema:
            name = str(schema["$ref"]).rsplit("/", 1)[-1]
            if name not in defs:
                raise ValueError(f"unresolvable $ref {schema['$ref']!r}")
            if name in seen:
                target = defs[name]
                target_type = target.get("type") if isinstance(target, Mapping) else None
                return {"type": target_type} if isinstance(target_type, str) else {}
            target = _dereference(defs[name], defs, seen | {name})
            siblings = {k: _dereference(v, defs, seen) for k, v in schema.items() if k != "$ref"}
            return {**target, **siblings} if siblings else target
        # Pydantic adds OpenAPI's `discriminator` optimization alongside an `anyOf` whose branches
        # already carry the discriminating `const`. z.fromJSONSchema does not consume the OpenAPI
        # keyword; dropping it preserves the exact accepted values while keeping the validator's
        # input pure JSON Schema.
        return {
            k: _dereference(v, defs, seen)
            for k, v in schema.items()
            if k not in ("$defs", "definitions", "discriminator")
        }
    if isinstance(schema, list):
        return [_dereference(item, defs, seen) for item in schema]
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


def _unwrap_fastmcp_result_envelope(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Drop FastMCP's ``{result: …}`` envelope from a non-object tool output schema.

    FastMCP wraps non-dict returns (a scalar, list, or ``None``) in a ``{result: …}`` object and
    flags it with ``x-fastmcp-wrap-result`` (``tools/function_parsing.py``); the wire
    ``structuredContent`` carries that envelope, and the frontend's ``unwrapToolResult`` undoes it
    before dispatching to a widget. The schema a result widget validates against is therefore the
    inner value, not the envelope — unwrap it here so the emitted catalog describes what the widget
    sees. A no-op for object outputs (Pydantic models) and for input schemas, which are never
    wrapped. Run after ``_inline_schema_refs`` so ``$ref``s inside the inner value are resolved
    against the envelope's ``$defs`` first.
    """
    if schema.get("x-fastmcp-wrap-result") is True:
        props = schema.get("properties")
        if isinstance(props, Mapping) and set(props) == {"result"}:
            inner = props["result"]
            if isinstance(inner, Mapping):
                return dict(inner)
    return dict(schema)


def _frontend_schema(schema: Mapping[str, Any], path: str) -> dict[str, Any]:
    """Inline a tool schema's ``$ref``s, drop FastMCP's result-wrap envelope, and validate it.

    The same processing for an input schema and an output schema: both are JSON-Schema objects the
    frontend's ``z.fromJSONSchema`` adapter must be able to represent.
    """
    inlined = _inline_schema_refs(schema)
    unwrapped = _unwrap_fastmcp_result_envelope(inlined)
    _validate_frontend_schema(unwrapped, path)
    return unwrapped


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
            tool_properties[tool.name] = _frontend_schema(
                input_schema, f"$.properties.{server_id}.properties.{tool.name}"
            )

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


async def build_mcp_tool_results_schema() -> dict[str, Any]:
    """Return one deterministic JSON Schema catalog of each tool's advertised output schema.

    The output-side mirror of :func:`build_mcp_tool_arguments_schema`. Every reflected server's
    tools carry trusted return types — the in-process servers' ducktape Pydantic models and
    ``grocy-sf``'s custom batch tools (also ducktape Pydantic models in ``grocy_mcp.mcp_types``) —
    so the ``outputSchema`` FastMCP derives from them is a reliable single source of truth. The
    grocy-sf OpenAPI tools are not reflected here at all: the exporter builds only the batch tools,
    and grocy-sf strips output schemas from its OpenAPI tools at runtime anyway
    (``grocy_mcp/server.py``) because Grocy's response shapes are unreliable. ``grocy-sf`` is
    limited to ``_SERVER_TOOL_ALLOWLIST`` just like its argument schemas; ``get_system_info`` (an
    OpenAPI tool with no batch counterpart) therefore has no generated result schema and its widget
    stays hand-authored. FastMCP publishes an output schema for every tool (a ``-> None`` return is
    wrapped as a null ``{result: null}``); such null results have no structured value to render, so
    they are omitted and a server's result tool set is a subset of its argument tool set.
    """

    server_properties: dict[str, Any] = {}
    for server_id, server in build_schema_servers().items():
        allowlist = _SERVER_TOOL_ALLOWLIST.get(server_id)
        async with Client(server) as client:
            tools = sorted(await client.list_tools(), key=lambda tool: tool.name)

        tool_properties: dict[str, Any] = {}
        for tool in tools:
            if allowlist is not None and tool.name not in allowlist:
                continue
            output_schema = tool.outputSchema
            if output_schema is None:
                continue
            if not isinstance(output_schema, dict):
                raise ValueError(f"{server_id}.{tool.name} published a non-object output schema")
            schema = _frontend_schema(output_schema, f"$.properties.{server_id}.properties.{tool.name}")
            if schema.get("type") == "null":
                continue  # `-> None` return: nothing structured to render
            tool_properties[tool.name] = schema

        if allowlist is not None and set(tool_properties) != set(allowlist):
            missing = ", ".join(sorted(allowlist - set(tool_properties)))
            raise ValueError(f"{server_id} is missing allowlisted preview tools: {missing}")

        server_properties[server_id] = {
            "type": "object",
            "additionalProperties": False,
            "properties": tool_properties,
            "required": list(tool_properties),
        }

    console_tool_properties = {
        tool_name: _frontend_schema(
            model.model_json_schema(mode="serialization"), f"$.properties.{SERVER_NAME}.properties.{tool_name}"
        )
        for tool_name, model in _CONSOLE_NATIVE_RESULT_MODELS.items()
    }
    server_properties[SERVER_NAME] = {
        "type": "object",
        "additionalProperties": False,
        "properties": console_tool_properties,
        "required": list(console_tool_properties),
    }

    return {
        "$schema": _DRAFT_2020_12,
        "title": "McpToolResults",
        "type": "object",
        "additionalProperties": False,
        "properties": server_properties,
        "required": list(server_properties),
    }


async def export_mcp_tool_schemas_json() -> str:
    return json.dumps(await build_mcp_tool_arguments_schema(), indent=2, sort_keys=True) + "\n"


async def export_mcp_tool_results_json() -> str:
    return json.dumps(await build_mcp_tool_results_schema(), indent=2, sort_keys=True) + "\n"


async def _main() -> None:
    # The same exporter serves both frontend catalogs: the arguments catalog by default, the
    # results catalog with `--results`. `js_json_schema` invokes this binary with no other args.
    match sys.argv[1:]:
        case ["--results"]:
            print(await export_mcp_tool_results_json(), end="")
        case []:
            print(await export_mcp_tool_schemas_json(), end="")
        case _:
            raise SystemExit(f"unexpected arguments: {' '.join(sys.argv[1:])}")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
