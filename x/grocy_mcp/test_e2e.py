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
    # Generic entity CRUD
    "entities_create",
    "entities_list",
    "entities_get",
    # Stock overview
    "stock_get",
    # Stock mutations
    "stock_add",
    "stock_consume",
    "stock_set",
    "stock_transfer",
    # Stock entries
    "stock_entries_list",
    "stock_entry_edit",
    # Reference data — read
    "products_list",
    "locations_list",
    "quantity_units_list",
    "product_groups_list",
    # Reference data — typed batch creators
    "products_create",
    "locations_create",
    "quantity_units_create",
    "product_groups_create",
    "shopping_lists_list",
    "shopping_lists_create",
    # Product mutations
    "product_edit",
    "product_delete",
    # Shopping list
    "shopping_list_get",
    "shopping_list_items_add",
    "shopping_list_item_edit",
    "shopping_list_items_remove",
    "shopping_list_clear",
    # Volatile stock queries
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
    """Full workflow: reference data -> create products -> stock operations -> entries -> shopping list."""
    suffix = uuid.uuid4().hex[:6]

    # List reference data (brief mode)
    _data(await mcp_client.call_tool("product_groups_list", {"detail": "brief"}))

    # Create a product group via the typed batch creator and verify round-trip
    new_group_name = f"TestGroupTyped-{suffix}"
    group_results = _data(
        await mcp_client.call_tool(
            "product_groups_create", {"items": [{"name": new_group_name, "description": "e2e fixture group"}]}
        )
    )
    assert group_results[0]["kind"] == "ok", f"product_groups_create failed: {group_results[0]}"
    groups = _data(await mcp_client.call_tool("product_groups_list", {"detail": "brief"}))
    assert new_group_name in [g["name"] for g in groups]

    # Create a shopping list and verify it appears in shopping_lists_list
    new_list_name = f"TestList-{suffix}"
    list_results = _data(
        await mcp_client.call_tool(
            "shopping_lists_create", {"items": [{"name": new_list_name, "description": "e2e fixture list"}]}
        )
    )
    assert list_results[0]["kind"] == "ok", f"shopping_lists_create failed: {list_results[0]}"
    shopping_lists = _data(await mcp_client.call_tool("shopping_lists_list", {"detail": "brief"}))
    assert new_list_name in [sl["name"] for sl in shopping_lists]

    # Create a fresh location and quantity unit via the typed batch creators
    new_loc_name = f"TestPantry-{suffix}"
    new_qu_name = f"TestBag-{suffix}"
    loc_results = _data(
        await mcp_client.call_tool(
            "locations_create",
            {"items": [{"name": new_loc_name, "description": "e2e fixture location", "is_freezer": False}]},
        )
    )
    assert loc_results[0]["kind"] == "ok", f"locations_create failed: {loc_results[0]}"
    qu_results = _data(
        await mcp_client.call_tool(
            "quantity_units_create",
            {"items": [{"name": new_qu_name, "name_plural": f"{new_qu_name}s", "description": "e2e fixture QU"}]},
        )
    )
    assert qu_results[0]["kind"] == "ok", f"quantity_units_create failed: {qu_results[0]}"

    # Verify the new location and QU show up in their list_* tools
    locations = _data(await mcp_client.call_tool("locations_list", {"detail": "brief"}))
    assert new_loc_name in [loc["name"] for loc in locations]
    qus = _data(await mcp_client.call_tool("quantity_units_list", {"detail": "brief"}))
    assert new_qu_name in [q["name"] for q in qus]

    loc_name = new_loc_name
    qu_name = new_qu_name

    # Batch-create two products using the typed products_create tool
    create_results = _data(
        await mcp_client.call_tool(
            "products_create",
            {
                "items": [
                    {
                        "name": f"TestRice-{suffix}",
                        "stock_qu": qu_name,
                        "location": loc_name,
                        "min_stock_amount": 1,
                        "description": "Test product for e2e",
                    },
                    {"name": f"TestFlour-{suffix}", "stock_qu": qu_name, "location": loc_name},
                ]
            },
        )
    )
    assert all(r["kind"] == "ok" for r in create_results), f"products_create failed: {create_results}"
    product_id = create_results[0]["created_object_id"]
    assert create_results[1]["created_object_id"] is not None

    # Verify both products appear in products_list
    products = _data(await mcp_client.call_tool("products_list", {"detail": "brief"}))
    product_names = [p["name"] for p in products]
    assert f"TestRice-{suffix}" in product_names
    assert f"TestFlour-{suffix}" in product_names

    # Add stock (name-based references)
    ops = _data(
        await mcp_client.call_tool(
            "stock_add",
            {"items": [{"product": f"TestRice-{suffix}", "amount": 5, "qu": qu_name, "location": loc_name}]},
        )
    )
    op = ops[0]
    assert op["kind"] == "ok", f"stock_add failed: {op}"
    assert op["new_amount"] == 5.0
    assert op["qu_name"] == qu_name
    assert op["product_name"] == f"TestRice-{suffix}"
    assert op["location_name"] == loc_name

    # Get stock — compact response with product filter
    stock = _data(await mcp_client.call_tool("stock_get", {"products": [f"TestRice-{suffix}"]}))
    our_stock = [s for s in stock if s["product_name"] == f"TestRice-{suffix}"]
    assert len(our_stock) == 1
    assert float(our_stock[0]["amount"]) == 5.0
    assert our_stock[0]["qu_name"] == qu_name
    assert our_stock[0]["location_name"] == loc_name

    # Consume stock (name-based, location required)
    ops = _data(
        await mcp_client.call_tool(
            "stock_consume",
            {"items": [{"product": f"TestRice-{suffix}", "amount": 2, "qu": qu_name, "location": loc_name}]},
        )
    )
    assert ops[0]["kind"] == "ok", f"stock_consume failed: {ops[0]}"
    assert ops[0]["new_amount"] == 3.0
    assert ops[0]["qu_name"] == qu_name

    # Set absolute stock amount
    ops = _data(
        await mcp_client.call_tool(
            "stock_set",
            {"items": [{"product": f"TestRice-{suffix}", "new_amount": 10, "qu": qu_name, "location": loc_name}]},
        )
    )
    assert ops[0]["kind"] == "ok", f"stock_set failed: {ops[0]}"
    assert ops[0]["new_amount"] == 10.0

    # Get stock entries by product name
    entries = _data(await mcp_client.call_tool("stock_entries_list", {"products": [f"TestRice-{suffix}"]}))
    assert len(entries) > 0
    assert entries[0]["kind"] == "ok"
    detail = entries[0]["entry"]
    assert detail["product_name"] == f"TestRice-{suffix}"
    assert detail["qu_name"] == qu_name
    entry_id = detail["entry_id"]

    # Edit stock entry — partial update (change price only), verify changes diff
    edit_result = _data(await mcp_client.call_tool("stock_entry_edit", {"entry_id": entry_id, "price": 9.99}))
    assert edit_result["kind"] == "ok", f"stock_entry_edit failed: {edit_result}"
    assert float(edit_result["entry"]["price"]) == 9.99
    assert edit_result.get("changes") is not None, "edit should return changes diff"
    assert "price" in edit_result["changes"]
    assert float(edit_result["changes"]["price"]["new"]) == 9.99

    # Get stock entry by ID — verify edit landed
    entries = _data(await mcp_client.call_tool("stock_entries_list", {"entry_ids": [entry_id]}))
    assert entries[0]["kind"] == "ok"
    assert float(entries[0]["entry"]["price"]) == 9.99

    # Edit product — rename via partial update, verify other fields preserved
    new_name = f"TestRice-Renamed-{suffix}"
    edit_result = _data(await mcp_client.call_tool("product_edit", {"product": f"TestRice-{suffix}", "name": new_name}))
    assert edit_result["kind"] == "ok", f"product_edit failed: {edit_result}"

    # Verify rename landed AND other fields weren't clobbered
    products = _data(await mcp_client.call_tool("products_list", {"detail": "full"}))
    our_product = [p for p in products if p["name"] == new_name]
    assert len(our_product) == 1
    # min_stock_amount was set to 1 at creation — verify it survived the edit
    assert float(our_product[0]["min_stock_amount"]) == 1
    assert our_product[0]["description"] == "Test product for e2e"

    # Shopping list — add product-linked and note-only items
    sl_results = _data(
        await mcp_client.call_tool(
            "shopping_list_items_add",
            {
                "items": [
                    {"product": new_name, "amount": 2, "shopping_list": 1},
                    {"note": "Check paper towels", "shopping_list": 1},
                    # Product + note together: "Milk — get the organic brand"
                    {"product": new_name, "amount": 1, "note": "get the organic brand", "shopping_list": 1},
                ]
            },
        )
    )
    assert sl_results[0]["kind"] == "ok"
    assert sl_results[1]["kind"] == "ok"
    assert sl_results[2]["kind"] == "ok"
    sl_item_id = sl_results[0]["item_id"]

    # Get shopping list — verify items present (including the product+note one)
    sl_data = _data(await mcp_client.call_tool("shopping_list_get", {"shopping_list": 1}))
    assert "items" in sl_data
    our_items = [i for i in sl_data["items"] if i.get("product_name") == new_name]
    assert len(our_items) >= 2
    product_plus_note = [i for i in our_items if i.get("note") == "get the organic brand"]
    assert len(product_plus_note) == 1, f"product+note item missing: {our_items}"

    # Edit shopping list item — mark as done
    edit_sl = _data(await mcp_client.call_tool("shopping_list_item_edit", {"item_id": sl_item_id, "done": True}))
    assert edit_sl["kind"] == "ok"

    # Remove from shopping list
    _data(await mcp_client.call_tool("shopping_list_items_remove", {"item_ids": [sl_item_id]}))

    # Query tools — exercise endpoints (may return empty, just verify no error)
    _data(await mcp_client.call_tool("get_expiring_stock", {"days_ahead": 30}))
    _data(await mcp_client.call_tool("get_below_minimum_stock", {}))
    _data(await mcp_client.call_tool("get_expired_stock", {}))

    # System tools
    _data(await mcp_client.call_tool("get_db_changed_time", {}))

    # Generic entity CRUD path: entities_create + entities_get (writeable type),
    # plus entities_list against a read-only view (`stock`) for the read/write split.
    pg_create = _data(
        await mcp_client.call_tool(
            "entities_create",
            {"items": [{"entity_type": "product_groups", "body": {"name": f"TestGroupGeneric-{suffix}"}}]},
        )
    )
    assert pg_create[0]["kind"] == "ok"
    pg_id = pg_create[0]["created_object_id"]
    pg_fetched = _data(
        await mcp_client.call_tool("entities_get", {"entity_type": "product_groups", "object_ids": [pg_id]})
    )
    assert pg_fetched[0]["kind"] == "ok"
    assert pg_fetched[0]["data"]["name"] == f"TestGroupGeneric-{suffix}"
    # Read-only entity types are accepted by entities_list.
    stock_rows = _data(await mcp_client.call_tool("entities_list", {"entity_types": ["stock"]}))
    assert "stock" in stock_rows

    entities = _data(
        await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [product_id]})
    )
    assert entities[0]["kind"] == "ok"
    assert entities[0]["data"]["name"] == new_name

    # Cleanup — delete both products (location/QU stay; harmless across runs).
    for name in (new_name, f"TestFlour-{suffix}"):
        del_result = _data(await mcp_client.call_tool("product_delete", {"product": name}))
        assert del_result["kind"] == "ok", f"product_delete({name}) failed: {del_result}"


# -- Read / write entity-type split ------------------------------------------


async def test_create_entities_rejects_view_only_type(mcp_client: Client) -> None:
    """`entities_create` only accepts `WriteableEntityType`.

    Calling it with a read-only entity type (e.g. `stock` — a computed
    view, no underlying writeable table) is rejected at the schema
    layer before the request even leaves FastMCP.
    """
    with pytest.raises(Exception, match="entity_type"):
        await mcp_client.call_tool("entities_create", {"items": [{"entity_type": "stock", "body": {"amount": 1}}]})


async def test_list_entities_accepts_view_only_type(mcp_client: Client) -> None:
    """`entities_list` accepts the broader `ReadableEntityType` set.

    The same `stock` value `entities_create` rejects is fine here,
    along with the other view-only / computed entities.
    """
    rows = _data(
        await mcp_client.call_tool(
            "entities_list", {"entity_types": ["stock", "products_last_purchased", "permission_hierarchy"]}
        )
    )
    assert {"stock", "products_last_purchased", "permission_hierarchy"} <= set(rows)


if __name__ == "__main__":
    pytest_bazel.main()
