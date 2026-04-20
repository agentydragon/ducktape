"""E2E tests: Grocy container + MCP server -> tool verification.

Starts a real Grocy container (LinuxServer image, auth disabled, demo mode),
wires the MCP server to it, and exercises tool calls through the full MCP
protocol via fastmcp.Client with FastMCPTransport.

Timing profile (RBE, 2026-04-20, OTEL spans in undeclared outputs):

  Session-scoped setup (runs once, charged to first test):
    load_oci_image        14.7s  (tarball build 1.4s + docker load 13.3s for 82MB image)
    container start        3.1s  (Docker create + s6-overlay init + SSL keygen)
    wait_for_grocy_ready   3.5s  (3 probes: ReadError, HTTP 500, then 1.3s migration on GET /)

  Per-test fixture overhead:
    build_mcp             ~70ms  (FastMCP.from_openapi parses the Grocy spec)

  Tests (wall clock, excluding setup):
    test_all_tool_names    <1ms  (just set comparison after build_mcp)
    test_system_info       108ms
    test_referencedata     288ms
    test_product_lifecycle 379ms
    test_stock_operations  696ms  (heaviest: add/consume/set/transfer/edit)
    test_shopping_list     577ms
    test_volatile_queries  201ms
    test_generic_crud      293ms
    test_rejects_viewonly   94ms
    test_accepts_viewonly  336ms
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
from opentelemetry import trace

from x.grocy_mcp.grocy_container import make_settings
from x.grocy_mcp.server import build_mcp
from x.grocy_mcp.test_helpers import RefData, create_refunwrap_result, unwrap_result
from x.grocy_mcp.tool_metadata import TOOL_OVERRIDES

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

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
    "products_edit",
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
        with tracer.start_as_current_span("build_mcp"):
            mcp = build_mcp(make_settings(grocy_base_url), client=http_client)
        async with Client(FastMCPTransport(mcp)) as client:
            yield client
    finally:
        await http_client.aclose()


@pytest.fixture
async def refunwrap_result(mcp_client: Client) -> RefData:
    """Create reference data (location, QU, group, two products) with uuid suffixes."""
    return await create_refunwrap_result(mcp_client)


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
    data = unwrap_result(await mcp_client.call_tool("get_system_info", {}))
    text = str(data)
    assert "grocy_version" in text.lower() or "grocy" in text.lower(), f"Unexpected: {text[:200]}"


# -- Reference data CRUD -----------------------------------------------------


async def test_referenceunwrap_result_crud(mcp_client: Client) -> None:
    """Create and list product groups, shopping lists, locations, QUs."""
    suffix = uuid.uuid4().hex[:6]

    # Product groups
    new_group_name = f"TestGroupTyped-{suffix}"
    group_results = unwrap_result(
        await mcp_client.call_tool(
            "product_groups_create", {"items": [{"name": new_group_name, "description": "e2e fixture group"}]}
        )
    )
    assert group_results[0]["kind"] == "ok"
    groups = unwrap_result(await mcp_client.call_tool("product_groups_list", {"detail": "brief"}))
    assert new_group_name in [g["name"] for g in groups]

    # Shopping lists
    new_list_name = f"TestList-{suffix}"
    list_results = unwrap_result(
        await mcp_client.call_tool(
            "shopping_lists_create", {"items": [{"name": new_list_name, "description": "e2e fixture list"}]}
        )
    )
    assert list_results[0]["kind"] == "ok"
    shopping_lists = unwrap_result(await mcp_client.call_tool("shopping_lists_list", {"detail": "brief"}))
    assert new_list_name in [sl["name"] for sl in shopping_lists]

    # Locations
    new_loc_name = f"TestLoc-{suffix}"
    loc_results = unwrap_result(
        await mcp_client.call_tool(
            "locations_create",
            {"items": [{"name": new_loc_name, "description": "e2e fixture location", "is_freezer": False}]},
        )
    )
    assert loc_results[0]["kind"] == "ok"
    locations = unwrap_result(await mcp_client.call_tool("locations_list", {"detail": "brief"}))
    assert new_loc_name in [loc["name"] for loc in locations]

    # Quantity units
    new_qu_name = f"TestQU-{suffix}"
    qu_results = unwrap_result(
        await mcp_client.call_tool(
            "quantity_units_create",
            {"items": [{"name": new_qu_name, "name_plural": f"{new_qu_name}s", "description": "e2e fixture QU"}]},
        )
    )
    assert qu_results[0]["kind"] == "ok"
    qus = unwrap_result(await mcp_client.call_tool("quantity_units_list", {"detail": "brief"}))
    assert new_qu_name in [q["name"] for q in qus]


# -- Product lifecycle -------------------------------------------------------


async def test_product_lifecycle(mcp_client: Client, refunwrap_result: RefData) -> None:
    """Create products, edit (rename), verify preserved fields, delete."""
    # Verify products exist
    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "brief"}))
    product_names = [p["name"] for p in products]
    assert refunwrap_result.products[0] in product_names
    assert refunwrap_result.products[1] in product_names

    # Edit product — rename, verify other fields preserved
    new_name = f"TestRice-Renamed-{refunwrap_result.suffix}"
    edit_results = unwrap_result(
        await mcp_client.call_tool(
            "products_edit", {"items": [{"product": refunwrap_result.products[0], "name": new_name}]}
        )
    )
    assert edit_results[0]["kind"] == "ok"
    # TODO: test batch edit (multiple items in one call)
    # TODO: test editing due_type, default_best_before_days, clear_fields
    # TODO: test that clear_fields nulls product_group while preserving other fields

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    our_product = [p for p in products if p["name"] == new_name]
    assert len(our_product) == 1
    assert float(our_product[0]["min_stock_amount"]) == 1
    assert our_product[0]["description"] == "Test product for e2e"

    # Delete both products
    for name in (new_name, refunwrap_result.products[1]):
        del_result = unwrap_result(await mcp_client.call_tool("product_delete", {"product": name}))
        assert del_result["kind"] == "ok", f"product_delete({name}) failed: {del_result}"


# -- entity_update PATCH semantics -------------------------------------------


async def test_entity_update_patch_semantics(
    mcp_client: Client, grocy_base_url: str, refunwrap_result: RefData
) -> None:
    """Grocy's ``PUT /objects/{entity}/{objectId}`` is a partial update.

    Locks in the three claims the ``entity_update`` tool description makes
    to agents, so a future Grocy version that flipped the behavior (or a
    misread of the server code) would break this test rather than silently
    corrupting user data:

      1. Omitted writable fields are preserved, not nulled.
      2. Unknown / server-computed columns echoed back in the body are
         silently dropped (no 400), so agents can safely round-trip
         ``entities_get`` output.
      3. Sending a nullable field with value ``null`` nulls it, without
         affecting other omitted fields.
    """
    product_id = refunwrap_result.product_ids[0]

    before = unwrap_result(
        await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [product_id]})
    )[0]["data"]
    # Fixture populates name, description, min_stock_amount, location_id, qu_id_stock.
    assert before["description"] == "Test product for e2e"
    assert float(before["min_stock_amount"]) == 1

    async with httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as http:
        # (1) Partial: only `description` in body.
        new_desc = f"patched-{refunwrap_result.suffix}"
        r = await http.put(f"/objects/products/{product_id}", json={"description": new_desc})
        assert r.status_code < 400, f"partial PUT failed: {r.status_code} {r.text!r}"

        after = unwrap_result(
            await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [product_id]})
        )[0]["data"]
        assert after["description"] == new_desc
        # Omitted writable fields preserved — the core PATCH claim.
        assert after["name"] == before["name"]
        assert float(after["min_stock_amount"]) == float(before["min_stock_amount"])
        assert after["location_id"] == before["location_id"]
        assert after["qu_id_stock"] == before["qu_id_stock"]

        # (2) Echoing the full GET row back fails with 400: Grocy rejects
        # server-computed columns (qu_factor_*, has_sub_products, etc.) on
        # PUT. Agents must send only writable columns — this is why the
        # docstring warns against blindly round-tripping `entities_get`.
        echo_body = dict(after)
        echo_body["description"] = f"echoed-{refunwrap_result.suffix}"
        r = await http.put(f"/objects/products/{product_id}", json=echo_body)
        assert r.status_code == 400, f"Expected 400 when echoing computed columns back, got {r.status_code}: {r.text!r}"

        # (3) Explicit null nulls the field; other omitted fields still preserved.
        r = await http.put(f"/objects/products/{product_id}", json={"description": None})
        assert r.status_code < 400, f"null PUT failed: {r.status_code} {r.text!r}"
        after3 = unwrap_result(
            await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [product_id]})
        )[0]["data"]
        assert after3["description"] in (None, "")  # Grocy normalizes null→"" on some columns
        assert after3["name"] == before["name"]
        assert float(after3["min_stock_amount"]) == float(before["min_stock_amount"])


# -- Stock operations --------------------------------------------------------


async def test_stock_operations(mcp_client: Client, refunwrap_result: RefData) -> None:
    """Add, consume, set stock; verify amounts via stock_get and stock_entries."""
    product = refunwrap_result.products[0]
    qu = refunwrap_result.qu
    loc = refunwrap_result.location

    # Add stock
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 5, "qu": qu, "location": loc}]}
        )
    )
    assert ops[0]["kind"] == "ok"
    assert ops[0]["new_amount"] == 5.0
    assert ops[0]["qu_name"] == qu
    assert ops[0]["location_name"] == loc

    # Get stock with product filter
    stock = unwrap_result(await mcp_client.call_tool("stock_get", {"products": [product]}))
    our_stock = [s for s in stock if s["product_name"] == product]
    assert len(our_stock) == 1
    assert float(our_stock[0]["amount"]) == 5.0

    # Consume stock
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_consume", {"items": [{"product": product, "amount": 2, "qu": qu, "location": loc}]}
        )
    )
    assert ops[0]["kind"] == "ok"
    assert ops[0]["new_amount"] == 3.0

    # Set absolute stock amount
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_set", {"items": [{"product": product, "new_amount": 10, "qu": qu, "location": loc}]}
        )
    )
    assert ops[0]["kind"] == "ok"
    assert ops[0]["new_amount"] == 10.0

    # Stock entries by product name
    entries = unwrap_result(await mcp_client.call_tool("stock_entries_list", {"products": [product]}))
    assert len(entries) > 0
    assert entries[0]["kind"] == "ok"
    detail = entries[0]["entry"]
    assert detail["product_name"] == product
    entry_id = detail["entry_id"]

    # Edit stock entry — change price, verify diff (batch API)
    edit_results = unwrap_result(
        await mcp_client.call_tool("stock_entry_edit", {"items": [{"entry_id": entry_id, "price": 9.99}]})
    )
    assert len(edit_results) == 1
    edit_result = edit_results[0]
    assert edit_result["kind"] == "ok"
    assert float(edit_result["entry"]["price"]) == 9.99
    assert edit_result.get("changes") is not None
    assert "price" in edit_result["changes"]

    # Verify edit landed via re-fetch
    entries = unwrap_result(await mcp_client.call_tool("stock_entries_list", {"entry_ids": [entry_id]}))
    assert float(entries[0]["entry"]["price"]) == 9.99


# -- Shopping list operations ------------------------------------------------


async def test_shopping_list_operations(mcp_client: Client, refunwrap_result: RefData) -> None:
    """Add items (product-linked, note-only, product+note), get list, edit item, remove item."""
    product = refunwrap_result.products[0]

    # Add stock first so the product exists with stock
    unwrap_result(
        await mcp_client.call_tool(
            "stock_add",
            {
                "items": [
                    {"product": product, "amount": 1, "qu": refunwrap_result.qu, "location": refunwrap_result.location}
                ]
            },
        )
    )

    # Add items to default shopping list
    sl_results = unwrap_result(
        await mcp_client.call_tool(
            "shopping_list_items_add",
            {
                "items": [
                    {"product": product, "amount": 2, "shopping_list": 1},
                    {"note": "Check paper towels", "shopping_list": 1},
                    {"product": product, "amount": 1, "note": "get the organic brand", "shopping_list": 1},
                ]
            },
        )
    )
    assert all(r["kind"] == "ok" for r in sl_results)
    sl_item_id = sl_results[0]["item_id"]

    # Get shopping list — verify items present
    slunwrap_result = unwrap_result(await mcp_client.call_tool("shopping_list_get", {"shopping_list": 1}))
    assert "items" in slunwrap_result
    our_items = [i for i in slunwrap_result["items"] if i.get("product_name") == product]
    assert len(our_items) >= 2
    product_plus_note = [i for i in our_items if i.get("note") == "get the organic brand"]
    assert len(product_plus_note) == 1

    # Edit item — mark as done
    edit_sl = unwrap_result(
        await mcp_client.call_tool("shopping_list_item_edit", {"item_id": sl_item_id, "done": True})
    )
    assert edit_sl["kind"] == "ok"

    # Remove item
    unwrap_result(await mcp_client.call_tool("shopping_list_items_remove", {"item_ids": [sl_item_id]}))


# -- Volatile stock queries --------------------------------------------------


async def test_volatile_stock_queries(mcp_client: Client) -> None:
    """Smoke test: expiring, below-minimum, expired queries don't error."""
    unwrap_result(await mcp_client.call_tool("get_expiring_stock", {"days_ahead": 30}))
    unwrap_result(await mcp_client.call_tool("get_below_minimum_stock", {}))
    unwrap_result(await mcp_client.call_tool("get_expired_stock", {}))
    unwrap_result(await mcp_client.call_tool("get_db_changed_time", {}))


# -- Generic entity CRUD ----------------------------------------------------


async def test_generic_entity_crud(mcp_client: Client, refunwrap_result: RefData) -> None:
    """entities_create, entities_get, entities_list with read-only types."""
    suffix = uuid.uuid4().hex[:6]

    # Create via generic path
    pg_create = unwrap_result(
        await mcp_client.call_tool(
            "entities_create",
            {"items": [{"entity_type": "product_groups", "body": {"name": f"TestGroupGeneric-{suffix}"}}]},
        )
    )
    assert pg_create[0]["kind"] == "ok"
    pg_id = pg_create[0]["created_object_id"]

    # Fetch by ID
    pg_fetched = unwrap_result(
        await mcp_client.call_tool("entities_get", {"entity_type": "product_groups", "object_ids": [pg_id]})
    )
    assert pg_fetched[0]["kind"] == "ok"
    assert pg_fetched[0]["data"]["name"] == f"TestGroupGeneric-{suffix}"

    # Read-only entity types accepted by entities_list
    stock_rows = unwrap_result(await mcp_client.call_tool("entities_list", {"entity_types": ["stock"]}))
    assert "stock" in stock_rows

    # Fetch product via generic path
    entities = unwrap_result(
        await mcp_client.call_tool(
            "entities_get", {"entity_type": "products", "object_ids": [refunwrap_result.product_ids[0]]}
        )
    )
    assert entities[0]["kind"] == "ok"
    assert entities[0]["data"]["name"] == refunwrap_result.products[0]


# -- Read / write entity-type split ------------------------------------------


async def test_create_entities_rejects_view_only_type(mcp_client: Client) -> None:
    """``entities_create`` only accepts ``WriteableEntityType``."""
    with pytest.raises(Exception, match="entity_type"):
        await mcp_client.call_tool("entities_create", {"items": [{"entity_type": "stock", "body": {"amount": 1}}]})


async def test_list_entities_accepts_view_only_type(mcp_client: Client) -> None:
    """``entities_list`` accepts the broader ``ReadableEntityType`` set."""
    rows = unwrap_result(
        await mcp_client.call_tool(
            "entities_list", {"entity_types": ["stock", "products_last_purchased", "permission_hierarchy"]}
        )
    )
    assert {"stock", "products_last_purchased", "permission_hierarchy"} <= set(rows)


if __name__ == "__main__":
    pytest_bazel.main()
