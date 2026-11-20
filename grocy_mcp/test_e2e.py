"""E2E tests: Grocy container + MCP server -> tool verification.

Starts a real Grocy container (LinuxServer image, auth disabled),
wires the MCP server to it, and exercises tool calls through the full MCP
protocol via fastmcp.Client with FastMCPTransport.

The session-scoped setup is charged to the first test: about 20 s on RBE (image load 15 s, container start
3 s, readiness 3.5 s; measured 2026-04-20).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import date, timedelta

import httpx
import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport
from opentelemetry import trace

from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.request_scoped_openapi import borrowed_http_client_provider
from grocy_mcp.server import build_mcp
from grocy_mcp.testing.ref_data import RefData, create_ref_data
from grocy_mcp.testing.results import unwrap_result

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@pytest.fixture
async def mcp_client(grocy_base_url: str) -> AsyncGenerator[Client]:
    """Function-scoped MCP client exercising the full MCP protocol in-process."""
    async with GrocyClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as http_client:
        with tracer.start_as_current_span("build_mcp"):
            mcp = build_mcp(
                ServerSettings(grocy_url=grocy_base_url), client_provider=borrowed_http_client_provider(http_client)
            )
        async with Client(FastMCPTransport(mcp)) as client:
            yield client


@pytest.fixture
async def ref_data(mcp_client: Client) -> RefData:
    """Create reference data (location, QU, group, two products) with uuid suffixes."""
    return await create_ref_data(mcp_client)


async def test_system_info_tool(mcp_client: Client) -> None:
    """GET /system/info is exposed as an MCP tool."""
    data = unwrap_result(await mcp_client.call_tool("get_system_info", {}))
    assert isinstance(data, dict)
    assert isinstance(data["grocy_version"], dict)
    version = data["grocy_version"]["Version"]
    assert isinstance(version, str)
    assert version


async def test_reference_data_crud(mcp_client: Client) -> None:
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


async def test_product_lifecycle(mcp_client: Client, ref_data: RefData) -> None:
    """Create products, edit (rename), verify preserved fields, delete."""
    # Verify products exist
    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "brief"}))
    product_names = [p["name"] for p in products]
    assert ref_data.products[0] in product_names
    assert ref_data.products[1] in product_names

    # Edit product — rename, verify other fields preserved
    new_name = f"TestRice-Renamed-{ref_data.suffix}"
    edit_results = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": ref_data.products[0], "name": new_name}]})
    )
    assert edit_results[0]["kind"] == "ok"
    # TODO: test batch edit (multiple items in one call)

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    our_product = [p for p in products if p["name"] == new_name]
    assert len(our_product) == 1
    assert float(our_product[0]["min_stock_amount"]) == 1
    assert our_product[0]["description"] == "Test product for e2e"

    # Delete both products
    for name in (new_name, ref_data.products[1]):
        del_result = unwrap_result(await mcp_client.call_tool("product_delete", {"product": name}))
        assert del_result["kind"] == "ok", f"product_delete({name}) failed: {del_result}"


async def test_products_create_requires_default_best_before_days(mcp_client: Client) -> None:
    """`products_create` rejects an item that omits `default_best_before_days`.

    Omission used to silently store 0 (best-before today), indistinguishable
    from a caller who deliberately chose same-day best-before — the field
    must be required with no default.
    """
    with pytest.raises(Exception, match="default_best_before_days"):
        await mcp_client.call_tool(
            "products_create", {"items": [{"name": "MissingBBD", "stock_qu": "Piece", "location": "Pantry"}]}
        )


async def test_products_edit_preserves_default_best_before_days_when_omitted(
    mcp_client: Client, ref_data: RefData
) -> None:
    """`products_edit` omitting `default_best_before_days` leaves it unchanged.

    Unlike `products_create` (which now requires the field explicitly),
    omission on edit means "leave the existing value alone" — the two
    endpoints must not share the same omission semantics.
    """
    product = ref_data.products[0]

    edit = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": product, "default_best_before_days": 42}]})
    )
    assert edit[0]["kind"] == "ok", edit

    # Edit an unrelated field, omitting default_best_before_days.
    unrelated_edit = unwrap_result(
        await mcp_client.call_tool(
            "products_edit", {"items": [{"product": product, "description": "unrelated change"}]}
        )
    )
    assert unrelated_edit[0]["kind"] == "ok", unrelated_edit

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    our_product = next(p for p in products if p["name"] == product)
    assert int(our_product["default_best_before_days"]) == 42


async def test_products_edit_serializes_values_and_resolves_references(mcp_client: Client, ref_data: RefData) -> None:
    """Keep scalar assignments, omission, clearing, and reference resolution at the Grocy boundary."""
    product_id = ref_data.product_ids[0]
    location_id = next(
        row["id"]
        for row in unwrap_result(await mcp_client.call_tool("locations_list", {}))
        if row["name"] == ref_data.location
    )
    quantity_unit_id = next(
        row["id"]
        for row in unwrap_result(await mcp_client.call_tool("quantity_units_list", {}))
        if row["name"] == ref_data.qu
    )
    product_group_id = next(
        row["id"]
        for row in unwrap_result(await mcp_client.call_tool("product_groups_list", {}))
        if row["name"] == ref_data.group
    )

    edit = unwrap_result(
        await mcp_client.call_tool(
            "products_edit",
            {
                "items": [
                    {
                        "product": product_id,
                        "stock_qu": ref_data.qu,
                        "location": location_id,
                        "purchase_qu": quantity_unit_id,
                        "consume_qu": ref_data.qu,
                        "parent_product": ref_data.product_ids[1],
                        "product_group": ref_data.group,
                        "min_stock_amount": 0,
                        "due_type": 2,
                        "description": "Edited product description",
                    }
                ]
            },
        )
    )
    assert edit[0]["kind"] == "ok", edit

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    product = next(row for row in products if int(row["id"]) == product_id)
    assert float(product["min_stock_amount"]) == 0
    assert int(product["due_type"]) == 2
    assert product["description"] == "Edited product description"
    assert int(product["qu_id_stock"]) == int(quantity_unit_id)
    assert int(product["location_id"]) == int(location_id)
    assert int(product["qu_id_purchase"]) == int(quantity_unit_id)
    assert int(product["qu_id_consume"]) == int(quantity_unit_id)
    assert int(product["parent_product_id"]) == ref_data.product_ids[1]
    assert int(product["product_group_id"]) == int(product_group_id)

    unrelated_edit = unwrap_result(
        await mcp_client.call_tool(
            "products_edit",
            {"items": [{"product": product_id, "name": f"Edited-{ref_data.suffix}", "description": None}]},
        )
    )
    assert unrelated_edit[0]["kind"] == "ok", unrelated_edit

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    product = next(row for row in products if int(row["id"]) == product_id)
    assert product["description"] == "Edited product description"
    assert float(product["min_stock_amount"]) == 0
    assert int(product["product_group_id"]) == int(product_group_id)

    empty_description = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": product_id, "description": ""}]})
    )
    assert empty_description[0]["kind"] == "ok", empty_description
    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    product = next(row for row in products if int(row["id"]) == product_id)
    # Grocy normalizes an empty product description to null on read.
    assert product["description"] is None

    clear = unwrap_result(
        await mcp_client.call_tool(
            "products_edit",
            {
                "items": [
                    {
                        "product": product_id,
                        "description": "clear_fields takes precedence",
                        "product_group": ref_data.group,
                        "clear_fields": ["description", "product_group"],
                    }
                ]
            },
        )
    )
    assert clear[0]["kind"] == "ok", clear

    products = unwrap_result(await mcp_client.call_tool("products_list", {"detail": "full"}))
    product = next(row for row in products if int(row["id"]) == product_id)
    assert product["description"] is None
    assert product["product_group_id"] is None
    assert float(product["min_stock_amount"]) == 0
    assert int(product["due_type"]) == 2


async def test_entity_update_patch_semantics(mcp_client: Client, grocy_base_url: str, ref_data: RefData) -> None:
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
    product_id = ref_data.product_ids[0]

    before = unwrap_result(
        await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [product_id]})
    )[0]["data"]
    # Fixture populates name, description, min_stock_amount, location_id, qu_id_stock.
    assert before["description"] == "Test product for e2e"
    assert float(before["min_stock_amount"]) == 1

    async with httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as http:
        # (1) Partial: only `description` in body.
        new_desc = f"patched-{ref_data.suffix}"
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
        echo_body["description"] = f"echoed-{ref_data.suffix}"
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


async def test_stock_operations(mcp_client: Client, ref_data: RefData) -> None:
    """Add, consume, set stock; verify amounts via stock_get and stock_entries."""
    product = ref_data.products[0]
    qu = ref_data.qu
    loc = ref_data.location

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


async def test_stock_entry_edit_serializes_partial_updates(mcp_client: Client, ref_data: RefData) -> None:
    """Partial edits preserve stored values, serialize dates, and apply explicit clears last."""
    product = ref_data.products[0]
    location = ref_data.location
    locations = unwrap_result(await mcp_client.call_tool("locations_list", {"detail": "full"}))
    location_id = next(row["id"] for row in locations if row["name"] == location)
    alternate_location = f"TestStockEdit-{ref_data.suffix}"
    alternate_result = unwrap_result(
        await mcp_client.call_tool("locations_create", {"items": [{"name": alternate_location}]})
    )
    assert alternate_result[0]["kind"] == "ok", alternate_result
    alternate_location_id = alternate_result[0]["created_object_id"]

    added = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 4, "qu": ref_data.qu, "location": location}]}
        )
    )
    assert added[0]["kind"] == "ok", added
    entry_id = added[0]["entry_id"]

    seeded = unwrap_result(
        await mcp_client.call_tool(
            "stock_entry_edit",
            {
                "items": [
                    {
                        "entry_id": entry_id,
                        "best_before_date": "2035-06-07",
                        "purchased_date": "2026-04-18",
                        "price": 8.5,
                        "location": alternate_location,
                        "open": True,
                        "note": "known stock-entry baseline",
                    }
                ]
            },
        )
    )[0]
    assert seeded["kind"] == "ok", seeded
    seeded_entry = seeded["entry"]
    assert seeded_entry["amount"] == 4.0
    assert seeded_entry["best_before_date"] == "2035-06-07"
    assert seeded_entry["purchased_date"] == "2026-04-18"
    assert seeded_entry["price"] == 8.5
    assert seeded_entry["location_name"] == alternate_location
    assert seeded_entry["open"] is True
    assert seeded_entry["note"] == "known stock-entry baseline"
    assert seeded["changes"]["location_id"]["new"] == alternate_location_id

    # Explicit nulls and omitted values both preserve the known non-null state.
    unchanged = unwrap_result(
        await mcp_client.call_tool(
            "stock_entry_edit", {"items": [{"entry_id": entry_id, "price": None, "purchased_date": None, "note": None}]}
        )
    )[0]
    assert unchanged["kind"] == "ok", unchanged
    assert unchanged.get("changes") is None
    assert unchanged["entry"] == seeded_entry

    updated = unwrap_result(
        await mcp_client.call_tool(
            "stock_entry_edit",
            {
                "items": [
                    {
                        "entry_id": entry_id,
                        "best_before_date": "2036-08-09",
                        "price": 0,
                        "location": location_id,
                        "open": False,
                    }
                ]
            },
        )
    )[0]
    assert updated["kind"] == "ok", updated
    updated_entry = updated["entry"]
    assert updated_entry["amount"] == 4.0
    assert updated_entry["best_before_date"] == "2036-08-09"
    assert updated_entry["purchased_date"] == "2026-04-18"
    assert updated_entry["price"] == 0.0
    assert updated_entry["location_name"] == location
    assert updated_entry["open"] is False
    assert updated_entry["note"] == "known stock-entry baseline"
    assert updated["changes"]["location_id"] == {"old": alternate_location_id, "new": location_id}

    cleared = unwrap_result(
        await mcp_client.call_tool(
            "stock_entry_edit",
            {
                "items": [
                    {
                        "entry_id": entry_id,
                        "price": 12.0,
                        "purchased_date": "2026-04-19",
                        "note": "cleared after assignment",
                        "location": alternate_location,
                        "clear_fields": ["price", "purchased_date", "note"],
                    }
                ]
            },
        )
    )[0]
    assert cleared["kind"] == "ok", cleared
    cleared_entry = cleared["entry"]
    assert cleared_entry["amount"] == 4.0
    assert cleared_entry["best_before_date"] == "2036-08-09"
    assert cleared_entry["open"] is False
    assert cleared_entry["purchased_date"] is None
    assert cleared_entry["price"] is None
    assert cleared_entry["location_name"] == alternate_location
    assert cleared_entry["note"] is None
    assert cleared["changes"]["price"]["new"] is None
    assert cleared["changes"]["purchased_date"]["new"] is None
    assert cleared["changes"]["note"]["new"] is None
    assert cleared["changes"]["location_id"]["new"] == alternate_location_id


async def test_stock_set_qu_optional_when_zeroing(mcp_client: Client, ref_data: RefData) -> None:
    """`qu` is optional on `stock_set` when `new_amount=0`; required otherwise.

    Motivated by the "nuke the Freezer" workflow: emptying a location bulk
    should not require the agent to preflight each product's `qu_id_stock`.
    For any nonzero amount, `qu` is still required (Grocy needs to know
    the unit for the add side of the correction).
    """
    product = ref_data.products[0]
    qu = ref_data.qu
    loc = ref_data.location

    # Seed: put something on the shelf.
    seed = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 7, "qu": qu, "location": loc}]}
        )
    )
    assert seed[0]["kind"] == "ok", seed

    # (1) Zero out without passing `qu` — succeeds, falls back to stock QU.
    zero_ops = unwrap_result(
        await mcp_client.call_tool("stock_set", {"items": [{"product": product, "new_amount": 0, "location": loc}]})
    )
    assert zero_ops[0]["kind"] == "ok", zero_ops
    assert zero_ops[0]["new_amount"] == 0.0
    # Fallback uses the product's own stock QU, which is the fixture's QU.
    assert zero_ops[0]["qu_name"] == qu

    # (2) Nonzero without `qu` — batch tools collect per-item errors rather
    # than raising, so the batch succeeds but this item reports an error.
    nonzero_ops = unwrap_result(
        await mcp_client.call_tool("stock_set", {"items": [{"product": product, "new_amount": 3, "location": loc}]})
    )
    assert nonzero_ops[0]["kind"] == "error", nonzero_ops

    # (3) Nonzero with `qu` — still works (regression check for the happy path).
    set_ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_set", {"items": [{"product": product, "new_amount": 3, "qu": qu, "location": loc}]}
        )
    )
    assert set_ops[0]["kind"] == "ok", set_ops
    assert set_ops[0]["new_amount"] == 3.0


async def test_stock_add_returns_entry_id(mcp_client: Client, ref_data: RefData) -> None:
    """`stock_add` echoes the new stock entry's `entry_id`, usable without a follow-up list call."""
    product = ref_data.products[0]
    qu = ref_data.qu
    loc = ref_data.location

    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": loc}]}
        )
    )
    assert ops[0]["kind"] == "ok", ops
    entry_id = ops[0]["entry_id"]
    assert entry_id is not None, ops

    entries = unwrap_result(await mcp_client.call_tool("stock_entries_list", {"entry_ids": [entry_id]}))
    assert entries[0]["kind"] == "ok", entries
    assert entries[0]["entry"]["product_name"] == product
    assert float(entries[0]["entry"]["amount"]) == 1.0


async def test_stock_add_applies_default_best_before_days(mcp_client: Client, ref_data: RefData) -> None:
    """`stock_add` without `best_before_date` honors the product's configured shelf life.

    The tool contracts "Omit to use the product's `default_best_before_days`",
    but Grocy v4.6.0 has a bug in `StockService::AddProduct`: when the
    destination is a freezer location and the product has not configured
    `default_best_before_days_after_freezing` (schema default 0), Grocy's
    freezing branch fires anyway (guard `>= -1` instead of the correct
    `> 0 || == -1`) and overwrites BBD with today — the product's
    `default_best_before_days` is never consulted. Our batch tool fills
    the date in client-side so the contract holds on both freezer and
    non-freezer locations.
    """
    product = ref_data.products[0]
    qu = ref_data.qu
    non_freezer_loc = ref_data.location

    async def _latest_bbd() -> str:
        entries = unwrap_result(await mcp_client.call_tool("stock_entries_list", {"products": [product]}))
        latest = max((e["entry"] for e in entries if e["kind"] == "ok"), key=lambda e: e["entry_id"])
        return str(latest["best_before_date"])

    # Case 1: non-freezer, default_best_before_days=300 → today + 300.
    edit = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": product, "default_best_before_days": 300}]})
    )
    assert edit[0]["kind"] == "ok", edit
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": non_freezer_loc}]}
        )
    )
    assert ops[0]["kind"] == "ok", ops
    expected_case1 = (date.today() + timedelta(days=300)).isoformat()
    assert ops[0]["best_before_date"] == expected_case1
    assert await _latest_bbd() == expected_case1

    # Case 2: non-freezer, default_best_before_days=-1 → 2999-12-31.
    edit = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": product, "default_best_before_days": -1}]})
    )
    assert edit[0]["kind"] == "ok", edit
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": non_freezer_loc}]}
        )
    )
    assert ops[0]["kind"] == "ok", ops
    assert ops[0]["best_before_date"] == "2999-12-31"
    assert await _latest_bbd() == "2999-12-31"

    # Case 3 (regression for the reported bug): freezer location,
    # default_best_before_days=300 and default_best_before_days_after_freezing=0
    # (the schema default). Without the client-side fallback Grocy would
    # return BBD=today; our tool must return today+300.
    freezer_name = f"TestFreezer-{ref_data.suffix}"
    freezer_create = unwrap_result(
        await mcp_client.call_tool("locations_create", {"items": [{"name": freezer_name, "is_freezer": True}]})
    )
    assert freezer_create[0]["kind"] == "ok", freezer_create
    edit = unwrap_result(
        await mcp_client.call_tool("products_edit", {"items": [{"product": product, "default_best_before_days": 300}]})
    )
    assert edit[0]["kind"] == "ok", edit
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": freezer_name}]}
        )
    )
    assert ops[0]["kind"] == "ok", ops
    expected = (date.today() + timedelta(days=300)).isoformat()
    assert ops[0]["best_before_date"] == expected
    actual = await _latest_bbd()
    assert actual == expected, (
        f"Freezer-location stock_add with default_best_before_days=300 and "
        f"default_best_before_days_after_freezing=0 returned BBD={actual}, expected {expected}. "
        f"Grocy's freezer branch is known buggy here; the MCP tool is supposed to compute the date client-side."
    )

    # Case 4: explicit override always wins, freezer or not.
    override = date.today() + timedelta(days=7)
    ops = unwrap_result(
        await mcp_client.call_tool(
            "stock_add",
            {
                "items": [
                    {
                        "product": product,
                        "amount": 1,
                        "qu": qu,
                        "location": freezer_name,
                        "best_before_date": override.isoformat(),
                    }
                ]
            },
        )
    )
    assert ops[0]["kind"] == "ok", ops
    assert ops[0]["best_before_date"] == override.isoformat()
    assert await _latest_bbd() == override.isoformat()


async def test_shopping_list_operations(mcp_client: Client, ref_data: RefData) -> None:
    """Add items (product-linked, note-only, product+note), get list, edit item, remove item."""
    product = ref_data.products[0]

    # Add stock first so the product exists with stock
    unwrap_result(
        await mcp_client.call_tool(
            "stock_add",
            {"items": [{"product": product, "amount": 1, "qu": ref_data.qu, "location": ref_data.location}]},
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
    sl_data = unwrap_result(await mcp_client.call_tool("shopping_list_get", {"shopping_list": 1}))
    assert "items" in sl_data
    our_items = [i for i in sl_data["items"] if i.get("product_name") == product]
    assert len(our_items) >= 2
    product_plus_note = [i for i in our_items if i.get("note") == "get the organic brand"]
    assert len(product_plus_note) == 1

    exact = unwrap_result(
        await mcp_client.call_tool("entities_get", {"entity_type": "shopping_list", "object_ids": [sl_item_id]})
    )
    assert len(exact) == 1
    assert exact[0]["kind"] == "ok"
    assert exact[0]["entity_type"] == "shopping_list"
    assert exact[0]["object_id"] == sl_item_id
    assert int(exact[0]["data"]["id"]) == sl_item_id
    assert float(exact[0]["data"]["amount"]) == 2
    assert exact[0]["shopping_item_names"]["product_name"] == product
    assert exact[0]["shopping_item_names"]["list_name"] == sl_data["name"]

    # Edit item — mark as done
    edit_sl = unwrap_result(
        await mcp_client.call_tool("shopping_list_item_edit", {"item_id": sl_item_id, "done": True})
    )
    assert edit_sl["kind"] == "ok"

    # Remove item
    unwrap_result(await mcp_client.call_tool("shopping_list_items_remove", {"item_ids": [sl_item_id]}))


async def test_volatile_stock_queries(mcp_client: Client) -> None:
    """Smoke test: expiring, below-minimum, expired queries don't error."""
    unwrap_result(await mcp_client.call_tool("get_expiring_stock", {"days_ahead": 30}))
    unwrap_result(await mcp_client.call_tool("get_below_minimum_stock", {}))
    unwrap_result(await mcp_client.call_tool("get_expired_stock", {}))
    unwrap_result(await mcp_client.call_tool("get_db_changed_time", {}))


async def test_generic_entity_crud(mcp_client: Client, ref_data: RefData) -> None:
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

    # Read-only entity types accepted by entities_list (the broader ``ReadableEntityType`` set)
    view_only = ["stock", "products_last_purchased", "permission_hierarchy"]
    rows = unwrap_result(await mcp_client.call_tool("entities_list", {"entity_types": view_only}))
    assert set(view_only) <= set(rows)

    # Fetch product via generic path
    entities = unwrap_result(
        await mcp_client.call_tool("entities_get", {"entity_type": "products", "object_ids": [ref_data.product_ids[0]]})
    )
    assert entities[0]["kind"] == "ok"
    assert entities[0]["data"]["name"] == ref_data.products[0]


async def test_create_entities_rejects_view_only_type(mcp_client: Client) -> None:
    """``entities_create`` only accepts ``WriteableEntityType``."""
    with pytest.raises(Exception, match="entity_type"):
        await mcp_client.call_tool("entities_create", {"items": [{"entity_type": "stock", "body": {"amount": 1}}]})


if __name__ == "__main__":
    pytest_bazel.main()
