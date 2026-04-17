"""E2E test: Grocy container + MCP server -> inventory bootstrap workflow.

Starts a real Grocy container (LinuxServer image, auth disabled, demo mode),
wires the MCP server to it, and exercises a realistic sequence of tool calls
that mirrors how an LLM would bootstrap a Grocy inventory from scratch.

All tool calls go through the full MCP protocol via fastmcp.Client with
FastMCPTransport. This ensures output schema validation and error propagation
match what real MCP clients (e.g. Claude) experience.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from x.grocy_mcp.grocy_container import make_settings
from x.grocy_mcp.server import build_mcp
from x.grocy_mcp.tool_metadata import TOOL_OVERRIDES

logger = logging.getLogger(__name__)

# Names of the custom batch tools registered by register_batch_tools.
CUSTOM_TOOL_NAMES = {
    "create_entities",
    "list_entities",
    "get_entities",
    "get_stock",
    "add_stock",
    "consume_stock",
    "inventory_stock",
    "get_stock_entries",
    "edit_stock_entry",
    # Phase 3 tools:
    "list_products",
    "list_locations",
    "list_quantity_units",
    "list_product_groups",
    "create_product",
    "edit_product",
    "delete_product",
    "transfer_stock",
    "get_shopping_list",
    "add_to_shopping_list",
    "edit_shopping_list_item",
    "remove_from_shopping_list",
    "clear_shopping_list",
    "get_expiring_stock",
    "get_below_minimum_stock",
    "get_expired_stock",
}


# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
async def mcp_client(grocy_base_url: str) -> AsyncGenerator[Client]:
    """Function-scoped MCP client exercising the full MCP protocol in-process."""
    http_client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    try:
        mcp = build_mcp(make_settings(grocy_base_url), client=http_client)
        async with Client(FastMCPTransport(mcp)) as client:
            yield client
    finally:
        await http_client.aclose()


def _data(result: Any) -> Any:
    """Extract unwrapped structured content from a FastMCP CallToolResult.

    FastMCP wraps non-object return types (lists, unions) in {"result": ...}
    with the x-fastmcp-wrap-result flag. The .data field has type-validated
    objects; .structured_content has raw dicts/lists. We use structured_content
    for test assertions (raw JSON-like access) and unwrap the synthetic wrapper.
    """
    sc = result.structured_content
    assert sc is not None, "No structured_content in CallToolResult"
    # Unwrap FastMCP's synthetic {"result": ...} wrapper for non-object schemas
    if isinstance(sc, dict) and len(sc) == 1 and "result" in sc:
        return sc["result"]
    return sc


# -- Tool name coverage ------------------------------------------------------


async def test_all_tool_names_are_customized(mcp_client: Client) -> None:
    """Every tool exposed by the MCP server has a name from TOOL_OVERRIDES or CUSTOM_TOOL_NAMES."""
    tools = await mcp_client.list_tools()
    expected_names = {o.name for o in TOOL_OVERRIDES.values() if o.enabled and not o.resource}
    expected_names |= CUSTOM_TOOL_NAMES
    actual_names = {t.name for t in tools}
    assert actual_names == expected_names, (
        f"Mismatch: extra={actual_names - expected_names}, missing={expected_names - actual_names}"
    )


# -- System info tool ---------------------------------------------------------


async def test_system_info_tool(mcp_client: Client) -> None:
    """GET /system/info is exposed as an MCP tool."""
    data = _data(await mcp_client.call_tool("get_system_info", {}))
    text = str(data)
    assert "grocy_version" in text.lower() or "grocy" in text.lower(), f"Unexpected: {text[:200]}"


# -- Inventory bootstrap workflow ---------------------------------------------


async def test_inventory_bootstrap(mcp_client: Client) -> None:
    """Full workflow: reference data -> create product -> stock operations -> entries -> shopping list."""
    suffix = uuid.uuid4().hex[:6]

    # 1. List reference data (brief mode)
    locations = _data(await mcp_client.call_tool("list_locations", {"detail": "brief"}))
    assert len(locations) > 0
    assert "id" in locations[0]
    assert "name" in locations[0]

    qus = _data(await mcp_client.call_tool("list_quantity_units", {"detail": "brief"}))
    assert len(qus) > 0
    assert "name_plural" in qus[0]

    _data(await mcp_client.call_tool("list_product_groups", {"detail": "brief"}))

    loc_name = str(locations[0]["name"])
    qu_name = str(qus[0]["name"])

    # 2. Create product using typed create_product tool
    result = _data(
        await mcp_client.call_tool(
            "create_product",
            {
                "name": f"TestRice-{suffix}",
                "stock_qu": qu_name,
                "location": loc_name,
                "min_stock_amount": 1,
                "description": "Test product for e2e",
            },
        )
    )
    assert result["kind"] == "ok", f"create_product failed: {result}"
    product_id = result["created_object_id"]

    # 3. Verify product appears in list_products
    products = _data(await mcp_client.call_tool("list_products", {"detail": "brief"}))
    assert f"TestRice-{suffix}" in [p["name"] for p in products]

    # 4. Add stock (name-based references)
    ops = _data(
        await mcp_client.call_tool(
            "add_stock",
            {"items": [{"product": f"TestRice-{suffix}", "amount": 5, "qu": qu_name, "location": loc_name}]},
        )
    )
    op = ops[0]
    assert op["kind"] == "ok", f"add_stock failed: {op}"
    assert op["new_amount"] == 5.0
    assert op["qu_name"] == qu_name
    assert op["product_name"] == f"TestRice-{suffix}"
    assert op["location_name"] == loc_name

    # 5. Get stock — compact response with product filter
    stock = _data(await mcp_client.call_tool("get_stock", {"products": [f"TestRice-{suffix}"]}))
    our_stock = [s for s in stock if s["product_name"] == f"TestRice-{suffix}"]
    assert len(our_stock) == 1
    assert float(our_stock[0]["amount"]) == 5.0
    assert our_stock[0]["qu_name"] == qu_name
    assert our_stock[0]["location_name"] == loc_name

    # 6. Consume stock (name-based, location required)
    ops = _data(
        await mcp_client.call_tool(
            "consume_stock",
            {"items": [{"product": f"TestRice-{suffix}", "amount": 2, "qu": qu_name, "location": loc_name}]},
        )
    )
    assert ops[0]["kind"] == "ok", f"consume_stock failed: {ops[0]}"
    assert ops[0]["new_amount"] == 3.0
    assert ops[0]["qu_name"] == qu_name

    # 7. Inventory — set absolute amount
    ops = _data(
        await mcp_client.call_tool(
            "inventory_stock",
            {"items": [{"product": f"TestRice-{suffix}", "new_amount": 10, "qu": qu_name, "location": loc_name}]},
        )
    )
    assert ops[0]["kind"] == "ok", f"inventory_stock failed: {ops[0]}"
    assert ops[0]["new_amount"] == 10.0

    # 8. Get stock entries by product name
    entries = _data(await mcp_client.call_tool("get_stock_entries", {"products": [f"TestRice-{suffix}"]}))
    assert len(entries) > 0
    assert entries[0]["kind"] == "ok"
    detail = entries[0]["entry"]
    assert detail["product_name"] == f"TestRice-{suffix}"
    assert detail["qu_name"] == qu_name
    entry_id = detail["entry_id"]

    # 9. Edit stock entry — partial update (change price only), verify changes diff
    edit_result = _data(await mcp_client.call_tool("edit_stock_entry", {"entry_id": entry_id, "price": 9.99}))
    assert edit_result["kind"] == "ok", f"edit_stock_entry failed: {edit_result}"
    assert float(edit_result["entry"]["price"]) == 9.99
    assert edit_result.get("changes") is not None, "edit should return changes diff"
    assert "price" in edit_result["changes"]
    assert float(edit_result["changes"]["price"]["new"]) == 9.99

    # 10. Get stock entry by ID — verify edit landed
    entries = _data(await mcp_client.call_tool("get_stock_entries", {"entry_ids": [entry_id]}))
    assert entries[0]["kind"] == "ok"
    assert float(entries[0]["entry"]["price"]) == 9.99

    # 11. Edit product — rename via partial update, verify other fields preserved
    new_name = f"TestRice-Renamed-{suffix}"
    edit_result = _data(await mcp_client.call_tool("edit_product", {"product": f"TestRice-{suffix}", "name": new_name}))
    assert edit_result["kind"] == "ok", f"edit_product failed: {edit_result}"

    # 12. Verify rename landed AND other fields weren't clobbered
    products = _data(await mcp_client.call_tool("list_products", {"detail": "full"}))
    our_product = [p for p in products if p["name"] == new_name]
    assert len(our_product) == 1
    # min_stock_amount was set to 1 at creation — verify it survived the edit
    assert float(our_product[0]["min_stock_amount"]) == 1
    assert our_product[0]["description"] == "Test product for e2e"

    # 13. Shopping list — add product-linked and note-only items
    sl_results = _data(
        await mcp_client.call_tool(
            "add_to_shopping_list",
            {
                "items": [
                    {"product": new_name, "amount": 2, "shopping_list": 1},
                    {"note": "Check paper towels", "shopping_list": 1},
                ]
            },
        )
    )
    assert sl_results[0]["kind"] == "ok"
    assert sl_results[1]["kind"] == "ok"
    sl_item_id = sl_results[0]["item_id"]

    # 14. Get shopping list — verify items present
    sl_data = _data(await mcp_client.call_tool("get_shopping_list", {"shopping_list": 1}))
    assert "items" in sl_data
    our_items = [i for i in sl_data["items"] if i.get("product_name") == new_name]
    assert len(our_items) >= 1

    # 15. Edit shopping list item — mark as done
    edit_sl = _data(await mcp_client.call_tool("edit_shopping_list_item", {"item_id": sl_item_id, "done": True}))
    assert edit_sl["kind"] == "ok"

    # 16. Remove from shopping list
    _data(await mcp_client.call_tool("remove_from_shopping_list", {"item_ids": [sl_item_id]}))

    # 17. Query tools — exercise endpoints (may return empty, just verify no error)
    _data(await mcp_client.call_tool("get_expiring_stock", {"days_ahead": 30}))
    _data(await mcp_client.call_tool("get_below_minimum_stock", {}))
    _data(await mcp_client.call_tool("get_expired_stock", {}))

    # 18. System tools
    _data(await mcp_client.call_tool("get_db_changed_time", {}))

    # 19. Generic entity CRUD still works
    entities = _data(
        await mcp_client.call_tool("get_entities", {"entity_type": "products", "object_ids": [product_id]})
    )
    assert entities[0]["kind"] == "ok"
    assert entities[0]["data"]["name"] == new_name

    # 20. Delete product — cleanup
    del_result = _data(await mcp_client.call_tool("delete_product", {"product": new_name}))
    assert del_result["kind"] == "ok"


if __name__ == "__main__":
    pytest_bazel.main()
