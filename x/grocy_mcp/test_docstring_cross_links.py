"""Static check: every `tool_name` mentioned in a tool docstring resolves to a real tool.

Catches typos and stale references after a rename. Runs against an
in-process MCP server built from the cached OpenAPI spec — no Grocy
container needed.
"""

from __future__ import annotations

import re

import httpx
import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import BaseModel

from x.grocy_mcp.grocy_types import PRODUCT_WRITABLE_FIELDS, EntityType, ReadableEntityType, WriteableEntityType
from x.grocy_mcp.mcp_types import (
    AddItem,
    BriefListItem,
    BriefQuantityUnit,
    ConsumeItem,
    CreateError,
    CreateItem,
    CreateLocationItem,
    CreateOk,
    CreateProductGroupItem,
    CreateProductItem,
    CreateQuantityUnitItem,
    CreateShoppingListItem,
    EditProductField,
    EditProductItem,
    EditShoppingListField,
    EditStockEntryField,
    EditStockEntryItem,
    FullLocation,
    FullProduct,
    FullProductGroup,
    FullQuantityUnit,
    FullShoppingList,
    GetError,
    GetOk,
    ServerSettings,
    SetItem,
    ShoppingItem,
    ShoppingListItemError,
    ShoppingListItemOk,
    StockEntry,
    StockEntryDetail,
    StockEntryError,
    StockEntryOk,
    StockOpError,
    StockOpOk,
)
from x.grocy_mcp.server import build_mcp

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

_PYDANTIC_MODELS: list[type[BaseModel]] = [
    AddItem,
    BriefListItem,
    BriefQuantityUnit,
    ConsumeItem,
    CreateError,
    CreateItem,
    CreateLocationItem,
    CreateOk,
    CreateProductGroupItem,
    CreateProductItem,
    CreateQuantityUnitItem,
    CreateShoppingListItem,
    EditProductItem,
    EditStockEntryItem,
    FullLocation,
    FullProduct,
    FullProductGroup,
    FullQuantityUnit,
    FullShoppingList,
    GetError,
    GetOk,
    SetItem,
    ShoppingItem,
    ShoppingListItemError,
    ShoppingListItemOk,
    StockEntry,
    StockEntryDetail,
    StockEntryError,
    StockEntryOk,
    StockOpError,
    StockOpOk,
]

_ENUMS = [
    EditProductField,
    EditShoppingListField,
    EditStockEntryField,
    EntityType,
    ReadableEntityType,
    WriteableEntityType,
]

# Tokens that appear backtick-quoted in tool descriptions but are not
# representable as Pydantic model fields or enum values — function parameter
# names, response dict keys, and OpenAPI tokens.
_RESIDUAL_TOKENS: set[str] = {
    # Function parameters and response dict keys not in Pydantic models
    "days_ahead",
    "days_overdue",
    "days_until_expiry",
    "deficit",
    "done",
    "entry_ids",
    "from_location",
    "min_amount",
    "to_location",
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

_KNOWN_NON_TOOL_TOKENS = (
    {field for model in _PYDANTIC_MODELS for field in model.model_fields}
    | {v.value for enum in _ENUMS for v in enum}
    | PRODUCT_WRITABLE_FIELDS
    | _RESIDUAL_TOKENS
)


async def test_docstring_cross_links_resolve() -> None:
    """Every backtick-quoted identifier in a tool description resolves to a live tool."""
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    async with httpx.AsyncClient(base_url=f"{settings.grocy_url}/api") as http_client:
        mcp = build_mcp(settings, client=http_client)
        async with Client(FastMCPTransport(mcp)) as client:
            tools = await client.list_tools()

    actual_names = {t.name for t in tools}
    tool_ref_re = re.compile(r"`([a-z][a-z0-9_]*)`")
    known_non_tools = _KNOWN_NON_TOOL_REFERENCES | _KNOWN_NON_TOOL_TOKENS

    def _refs_in(text: str) -> set[str]:
        return {tok for tok in tool_ref_re.findall(text) if tok not in known_non_tools}

    missing: dict[str, set[str]] = {}
    for tool in tools:
        candidates = _refs_in(tool.description or "")
        schema = tool.inputSchema or {}
        for prop in (schema.get("properties") or {}).values():
            candidates |= _refs_in(prop.get("description", "") or "")
        unresolved = candidates - actual_names
        if unresolved:
            missing[tool.name] = unresolved

    assert not missing, f"Tool docs reference unknown tool names: {missing}"


if __name__ == "__main__":
    pytest_bazel.main()
