"""Static check: every `tool_name` mentioned in a tool docstring resolves to a real tool.

Catches typos and stale references after a rename. Runs against an
in-process MCP server built from the cached OpenAPI spec — no Grocy
container needed.
"""

from __future__ import annotations

import re

import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from grocy_mcp.client import GrocyClient
from grocy_mcp.grocy_types import PRODUCT_WRITABLE_FIELDS
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.server import build_mcp
from mcp_infra.request_scoped_openapi import borrowed_http_client_provider

# Tool-ish identifiers that appear in docstrings but aren't live tools — MCP
# resources, type-alias names in cross-references, or FastMCP-generated
# non-tool handles.
_KNOWN_NON_TOOL_REFERENCES = {
    "WriteableEntityType",
    "ReadableEntityType",
    "EditStockEntryField",
    "EditProductField",
    "EditShoppingListField",
}

# Tokens that appear backtick-quoted in tool descriptions but that no tool's
# schema carries as a property name or enum value — function parameter names,
# response dict keys, and OpenAPI tokens.
_RESIDUAL_TOKENS: set[str] = {
    # Function parameters and response dict keys not in Pydantic models
    "days_ahead",
    "days_overdue",
    "days_until_expiry",
    "deficit",
    "done",
    "entry_ids",
    "factor",
    "from_location",
    "from_qu_id",
    "min_amount",
    "to_location",
    "to_qu_id",
    # OpenAPI-generated tokens not in our models
    "force_serve_as",
    "picture",
    # Server-computed columns referenced by the entity_update warning
    # (named in the "don't round-trip these" list — not writable).
    "has_sub_products",
    # Literal string/boolean values used as parameter values in descriptions
    "brief",
    "full",
    "true",
}


def _schema_tokens(node: object) -> set[str]:
    """Property names and string enum values anywhere in a JSON schema, `$defs` included."""
    match node:
        case dict():
            own = set(node.get("properties", {})) | {value for value in node.get("enum", []) if isinstance(value, str)}
            return own.union(*(_schema_tokens(child) for child in node.values()))
        case list():
            return set().union(*(_schema_tokens(child) for child in node))
        case _:
            return set()


async def test_docstring_cross_links_resolve() -> None:
    """Every backtick-quoted identifier in a tool description resolves to a live tool."""
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    async with GrocyClient(base_url=f"{settings.grocy_url}/api") as http_client:
        mcp = build_mcp(settings, client_provider=borrowed_http_client_provider(http_client))
        async with Client(FastMCPTransport(mcp)) as client:
            tools = await client.list_tools()

    actual_names = {t.name for t in tools}
    tool_ref_re = re.compile(r"`([a-z][a-z0-9_]*)`")
    known_non_tools = (
        _KNOWN_NON_TOOL_REFERENCES
        | PRODUCT_WRITABLE_FIELDS
        | _RESIDUAL_TOKENS
        | _schema_tokens([[tool.input_schema, tool.output_schema] for tool in tools])
    )

    def _refs_in(text: str) -> set[str]:
        return {tok for tok in tool_ref_re.findall(text) if tok not in known_non_tools}

    missing: dict[str, set[str]] = {}
    for tool in tools:
        candidates = _refs_in(tool.description or "")
        schema = tool.input_schema or {}
        for prop in (schema.get("properties") or {}).values():
            candidates |= _refs_in(prop.get("description", "") or "")
        unresolved = candidates - actual_names
        if unresolved:
            missing[tool.name] = unresolved

    assert not missing, f"Tool docs reference unknown tool names: {missing}"


if __name__ == "__main__":
    pytest_bazel.main()
