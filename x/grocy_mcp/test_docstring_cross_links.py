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

from x.grocy_mcp.config import ServerSettings
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

# Param / field / concept names that look like tools but aren't. Grouped for
# readability. Expand when a rename introduces a new term.
_KNOWN_NON_TOOL_TOKENS = {
    # Pydantic field names
    "items",
    "product",
    "products",
    "amount",
    "qu",
    "qu_name",
    "stock_qu",
    "purchase_qu",
    "location",
    "from_location",
    "to_location",
    "best_before_date",
    "purchased_date",
    "price",
    "note",
    "done",
    "shopping_list",
    "product_group",
    "name",
    "name_plural",
    "description",
    "is_freezer",
    "plural_forms",
    "min_stock_amount",
    "default_best_before_days",
    "spoiled",
    "allow_subproduct_substitution",
    "new_amount",
    "amount_delta",
    "transaction_id",
    "stock_qu_name",
    "location_name",
    "product_name",
    "item_id",
    "entry_id",
    "object_ids",
    "entry_ids",
    "item_ids",
    "entity_type",
    "entity_types",
    "body",
    "object_id",
    "data",
    "kind",
    "ok",
    "error",
    "changes",
    "result",
    "id",
    "detail",
    "brief",
    "full",
    "days_ahead",
    "deficit",
    "days_until_expiry",
    "days_overdue",
    "min_amount",
    "open",
    "clear_fields",
    "shopping_lists",
    "shopping_locations",
    # OpenAPI-generated query/response tokens referenced in tool descriptions
    "created_object_id",
    "force_serve_as",
    "picture",
    "asc",
    "desc",
    "true",
    # Entity-type values the docs reference as strings
    "locations",
    "quantity_units",
    "quantity_unit_conversions",
    "product_barcodes",
    "product_groups",
    "stock",
    "stock_log",
    "stock_current_locations",
    "products_last_purchased",
    "products_average_price",
    "permission_hierarchy",
    "chores_log",
    "battery_charge_cycles",
    "product_barcodes_view",
    "quantity_unit_conversions_resolved",
    "recipes_pos_resolved",
    "recipes",
    "recipes_pos",
    "recipes_nestings",
    "tasks",
    "task_categories",
    "chores",
    "batteries",
    "equipment",
    "userfields",
    "userentities",
    "userobjects",
    "api_keys",
    "meal_plan",
    "meal_plan_sections",
    # Domain terms / generic words
    "yyyy-mm-dd",
}


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
