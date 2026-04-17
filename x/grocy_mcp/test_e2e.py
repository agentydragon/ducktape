"""E2E test: Grocy container + MCP server → inventory bootstrap workflow.

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

import httpx
import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from x.grocy_mcp.grocy_fixtures import make_settings
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
    "inventory_products",
    "get_stock_entries",
    "edit_stock_entry",
}


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def mcp_client(grocy_base_url: str) -> AsyncGenerator[Client]:
    """Function-scoped MCP client exercising the full MCP protocol in-process.

    A fresh httpx.AsyncClient is created per test so connection pools don't carry
    sockets from a previous test's (now-closed) event loop. Session-scoping the
    httpx client caused "Event loop is closed" on the third test because asyncio
    transports are pinned to the loop that opened them.
    """
    http_client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    try:
        mcp = build_mcp(make_settings(grocy_base_url), client=http_client)
        async with Client(FastMCPTransport(mcp)) as client:
            yield client
    finally:
        await http_client.aclose()


# ── Tool name coverage ───────────────────────────────────────────────────


async def test_all_tool_names_are_customized(mcp_client: Client) -> None:
    """Every tool exposed by the MCP server has a name from TOOL_OVERRIDES or CUSTOM_TOOL_NAMES."""
    tools = await mcp_client.list_tools()
    expected_names = {o.name for o in TOOL_OVERRIDES.values() if o.enabled and not o.resource}
    expected_names |= CUSTOM_TOOL_NAMES
    actual_names = {t.name for t in tools}
    assert actual_names == expected_names, (
        f"Mismatch: extra={actual_names - expected_names}, missing={expected_names - actual_names}"
    )


# ── System info tool ─────────────────────────────────────────────────────


async def test_system_info_tool(mcp_client: Client) -> None:
    """GET /system/info is exposed as an MCP tool (claude.ai does not expose MCP resources to the AI)."""
    result = await mcp_client.call_tool("get_system_info", {})
    sc = result.structured_content
    assert sc is not None
    text = str(sc)
    assert "grocy_version" in text.lower() or "grocy" in text.lower(), f"Unexpected tool result: {text[:200]}"


# ── Inventory bootstrap workflow ─────────────────────────────────────────


async def test_inventory_bootstrap(mcp_client: Client) -> None:
    """Full batch inventory workflow: create entities → add → get enriched → consume → inventory."""
    suffix = uuid.uuid4().hex[:6]

    # 1. Create location and quantity unit in one batch call
    result = await mcp_client.call_tool(
        "create_entities",
        {
            "items": [
                {"entity_type": "locations", "body": {"name": f"TestLoc-{suffix}"}},
                {
                    "entity_type": "quantity_units",
                    "body": {"name": f"TestUnit-{suffix}", "name_plural": f"TestUnits-{suffix}"},
                },
            ]
        },
    )
    sc = result.structured_content
    assert sc is not None
    created = sc["result"]
    assert created[0]["kind"] == "ok", f"location create failed: {created[0].get('error')}"
    assert created[1]["kind"] == "ok", f"quantity_unit create failed: {created[1].get('error')}"
    loc_id = created[0]["created_object_id"]
    qu_id = created[1]["created_object_id"]
    qu_name = f"TestUnit-{suffix}"

    # 2. Create a product
    result = await mcp_client.call_tool(
        "create_entities",
        {
            "items": [
                {
                    "entity_type": "products",
                    "body": {
                        "name": f"TestRice-{suffix}",
                        "location_id": loc_id,
                        "qu_id_purchase": qu_id,
                        "qu_id_stock": qu_id,
                    },
                }
            ]
        },
    )
    sc = result.structured_content
    assert sc is not None
    product_create = sc["result"][0]
    assert product_create["kind"] == "ok", f"product create failed: {product_create.get('error')}"
    product_id = product_create["created_object_id"]
    assert product_id is not None

    # 3. Add stock (qu_name required)
    result = await mcp_client.call_tool(
        "add_stock",
        {"items": [{"product_id": product_id, "amount": 5, "best_before_date": "2030-01-01", "qu_name": qu_name}]},
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "ok", f"add_stock failed: {op.get('error')}"
    assert op["new_amount"] == 5.0
    assert op["qu_name"] == qu_name

    # 4. Get enriched stock — verify product + QU + location + qu_name
    result = await mcp_client.call_tool("get_stock", {"include_quantity_unit": True, "include_location": True})
    sc = result.structured_content
    assert sc is not None
    stock = sc["result"]
    product_stock = [s for s in stock if str(s["product_id"]) == str(product_id)]
    assert len(product_stock) == 1, f"product {product_id} not found in stock"
    assert float(product_stock[0]["amount"]) == 5.0
    assert product_stock[0]["qu_name"] == qu_name
    assert product_stock[0]["quantity_unit"] is not None, "quantity_unit not enriched"
    assert product_stock[0]["location"] is not None, "location not enriched"

    # 5. Consume some stock
    result = await mcp_client.call_tool(
        "consume_stock", {"items": [{"product_id": product_id, "amount": 2, "qu_name": qu_name}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "ok", f"consume_stock failed: {op.get('error')}"
    assert op["new_amount"] == 3.0
    assert op["qu_name"] == qu_name

    # 6. Inventory — set absolute amount
    result = await mcp_client.call_tool(
        "inventory_products", {"items": [{"product_id": product_id, "new_amount": 10, "qu_name": qu_name}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "ok", f"inventory_products failed: {op.get('error')}"
    assert op["new_amount"] == 10.0
    assert op["qu_name"] == qu_name

    # 7. List entities — fetch products, locations, quantity_units in one call
    result = await mcp_client.call_tool("list_entities", {"entity_types": ["products", "locations", "quantity_units"]})
    data = result.structured_content
    assert data is not None
    assert "products" in data
    assert "locations" in data
    assert "quantity_units" in data
    product_names = [p["name"] for p in data["products"]]
    assert f"TestRice-{suffix}" in product_names

    # 8. Get specific entity by ID
    result = await mcp_client.call_tool("get_entities", {"entity_type": "products", "object_ids": [product_id]})
    sc = result.structured_content
    assert sc is not None
    entities = sc["result"]
    assert entities[0]["kind"] == "ok", f"get_entities failed: {entities[0].get('error')}"
    assert entities[0]["data"]["name"] == f"TestRice-{suffix}"

    # 9. List product stock entries — get entry IDs
    result = await mcp_client.call_tool("list_product_stock_entries", {"productId": product_id})
    sc = result.structured_content
    assert sc is not None
    entries = sc if isinstance(sc, list) else sc.get("result", sc)
    assert len(entries) > 0, "no stock entries found"
    entry_id = int(entries[0]["id"])

    # 10. Get stock entries (batch) — verify entry + qu_name
    result = await mcp_client.call_tool("get_stock_entries", {"entry_ids": [entry_id]})
    sc = result.structured_content
    assert sc is not None
    entry_result = sc["result"][0]
    assert entry_result["kind"] == "ok", f"get_stock_entries failed: {entry_result.get('error')}"
    assert entry_result["qu_name"] == qu_name
    assert entry_result["entry_id"] == entry_id
    original_entry = entry_result["data"]

    # 11. Edit stock entry — update price, set open=true
    result = await mcp_client.call_tool(
        "edit_stock_entry",
        {
            "entry_id": entry_id,
            "amount": float(original_entry["amount"]),
            "best_before_date": original_entry["best_before_date"],
            "purchased_date": original_entry.get("purchased_date", "2030-01-01"),
            "price": 9.99,
            "location_id": loc_id,
            "open": True,
            "qu_name": qu_name,
        },
    )
    sc = result.structured_content
    assert sc is not None
    edit_result = sc.get("result", sc)
    assert edit_result["kind"] == "ok", f"edit_stock_entry failed: {edit_result.get('error')}"
    assert edit_result["qu_name"] == qu_name

    # 12. Get stock entry again — verify edit landed
    result = await mcp_client.call_tool("get_stock_entries", {"entry_ids": [entry_id]})
    sc = result.structured_content
    assert sc is not None
    entry_result = sc["result"][0]
    assert entry_result["kind"] == "ok"
    assert float(entry_result["data"]["price"]) == 9.99
    assert entry_result["data"]["open"] in (True, 1, "1"), f"open not set: {entry_result['data']['open']}"

    # 13. Get product stock — verify product-level view works
    result = await mcp_client.call_tool("get_product_stock", {"productId": product_id})
    sc = result.structured_content
    assert sc is not None

    # 14. List location stock — verify location-level view works
    result = await mcp_client.call_tool("list_location_stock", {"locationId": loc_id})
    sc = result.structured_content
    assert sc is not None

    # 15. List volatile stock — verify endpoint works (may return empty)
    result = await mcp_client.call_tool("list_volatile_stock", {})
    sc = result.structured_content
    assert sc is not None

    # 16. Update entity — rename product
    new_name = f"TestRice-Renamed-{suffix}"
    result = await mcp_client.call_tool(
        "update_entity", {"entity": "products", "objectId": product_id, "body": {"name": new_name}}
    )
    # update_entity is OpenAPI-generated, response varies

    # 17. Verify rename via get_entities
    result = await mcp_client.call_tool("get_entities", {"entity_type": "products", "object_ids": [product_id]})
    sc = result.structured_content
    assert sc is not None
    assert sc["result"][0]["data"]["name"] == new_name

    # 18. Get db changed time — verify system tool
    result = await mcp_client.call_tool("get_db_changed_time", {})
    sc = result.structured_content
    assert sc is not None

    # 19. Delete entity — cleanup
    result = await mcp_client.call_tool("delete_entity", {"entity": "products", "objectId": product_id})


if __name__ == "__main__":
    pytest_bazel.main()
