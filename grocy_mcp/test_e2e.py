"""E2E tests: Grocy container + MCP server -> tool verification.

Starts a real Grocy container (LinuxServer image, auth disabled),
wires the MCP server to it, and exercises tool calls through the full MCP
protocol via fastmcp.Client with FastMCPTransport.

The session-scoped setup is charged to the first test: about 20 s on RBE (image load 15 s, container start
3 s, readiness 3.5 s; measured 2026-04-20).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta

import httpx
import httpx2
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
from grocy_mcp.testing.results import call_tool

tracer = trace.get_tracer(__name__)


@pytest.fixture
async def grocy_http_client(grocy_base_url: str) -> AsyncGenerator[GrocyClient]:
    async with GrocyClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as client:
        yield client


@pytest.fixture
async def mcp_client(grocy_base_url: str, grocy_http_client: GrocyClient) -> AsyncGenerator[Client]:
    """Function-scoped MCP client exercising the full MCP protocol in-process."""
    with tracer.start_as_current_span("build_mcp"):
        mcp = build_mcp(
            ServerSettings(grocy_url=grocy_base_url), client_provider=borrowed_http_client_provider(grocy_http_client)
        )
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def ref_data(mcp_client: Client) -> RefData:
    """Create reference data (location, QU, group, two products) with uuid suffixes."""
    return await create_ref_data(mcp_client)


async def test_system_info_tool(mcp_client: Client) -> None:
    """GET /system/info is exposed as an MCP tool."""
    data = await call_tool(mcp_client, "get_system_info", {})
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
    group_results = await call_tool(
        mcp_client, "product_groups_create", {"items": [{"name": new_group_name, "description": "e2e fixture group"}]}
    )
    assert group_results[0]["kind"] == "ok"
    groups = await call_tool(mcp_client, "product_groups_list", {"detail": "brief"})
    assert new_group_name in [g["name"] for g in groups]

    # Shopping lists
    new_list_name = f"TestList-{suffix}"
    list_results = await call_tool(
        mcp_client, "shopping_lists_create", {"items": [{"name": new_list_name, "description": "e2e fixture list"}]}
    )
    assert list_results[0]["kind"] == "ok"
    shopping_lists = await call_tool(mcp_client, "shopping_lists_list", {"detail": "brief"})
    assert new_list_name in [sl["name"] for sl in shopping_lists]
    shopping_list = await call_tool(mcp_client, "shopping_list_get", {"shopping_list": new_list_name.lower()})
    assert shopping_list == {"name": new_list_name, "description": "e2e fixture list", "items": []}

    # Locations
    new_loc_name = f"TestLoc-{suffix}"
    loc_results = await call_tool(
        mcp_client,
        "locations_create",
        {"items": [{"name": new_loc_name, "description": "e2e fixture location", "is_freezer": False}]},
    )
    assert loc_results[0]["kind"] == "ok"
    locations = await call_tool(mcp_client, "locations_list", {"detail": "brief"})
    assert new_loc_name in [loc["name"] for loc in locations]

    # Quantity units
    new_qu_name = f"TestQU-{suffix}"
    qu_results = await call_tool(
        mcp_client,
        "quantity_units_create",
        {"items": [{"name": new_qu_name, "name_plural": f"{new_qu_name}s", "description": "e2e fixture QU"}]},
    )
    assert qu_results[0]["kind"] == "ok"
    qus = await call_tool(mcp_client, "quantity_units_list", {"detail": "brief"})
    assert new_qu_name in [q["name"] for q in qus]


async def test_product_lifecycle(mcp_client: Client, ref_data: RefData) -> None:
    """Create products, edit (rename), verify preserved fields, delete."""
    # Verify products exist
    products = await call_tool(mcp_client, "products_list", {"detail": "brief"})
    product_names = [p["name"] for p in products]
    assert ref_data.products[0] in product_names
    assert ref_data.products[1] in product_names

    # Edit product — rename, verify other fields preserved
    new_name = f"TestRice-Renamed-{ref_data.suffix}"
    edit_results = await call_tool(
        mcp_client, "products_edit", {"items": [{"product": ref_data.products[0], "name": new_name}]}
    )
    assert edit_results[0]["kind"] == "ok"
    # TODO: test batch edit (multiple items in one call)

    products = await call_tool(mcp_client, "products_list", {"detail": "full"})
    our_product = [p for p in products if p["name"] == new_name]
    assert len(our_product) == 1
    assert float(our_product[0]["min_stock_amount"]) == 1
    assert our_product[0]["description"] == "Test product for e2e"

    # Delete both products
    for name in (new_name, ref_data.products[1]):
        del_result = await call_tool(mcp_client, "product_delete", {"product": name})
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


async def test_products_edit_preserves_concurrent_shelf_life_change(
    mcp_client: Client, grocy_base_url: str, grocy_http_client: GrocyClient, ref_data: RefData
) -> None:
    product_id = ref_data.product_ids[0]
    path = f"/objects/products/{product_id}"
    async with httpx.AsyncClient(base_url=f"{grocy_base_url}/api") as other_writer:

        async def change_shelf_life(request: httpx2.Request) -> None:
            if request.method == "PUT" and request.url.path == f"/api{path}":
                response = await other_writer.put(path, json={"default_best_before_days": 42})
                response.raise_for_status()

        grocy_http_client.event_hooks["request"].append(change_shelf_life)
        edit = await call_tool(
            mcp_client, "products_edit", {"items": [{"product": product_id, "description": "New label"}]}
        )
        assert edit[0]["kind"] == "ok", edit
        stored = await other_writer.get(path)
        stored.raise_for_status()
        product = stored.json()
        assert product["description"] == "New label"
        assert int(product["default_best_before_days"]) == 42


async def test_products_edit_serializes_values_and_resolves_references(mcp_client: Client, ref_data: RefData) -> None:
    """Keep scalar assignments, omission, clearing, and reference resolution at the Grocy boundary."""
    product_id = ref_data.product_ids[0]
    location_id = next(
        row["id"] for row in await call_tool(mcp_client, "locations_list", {}) if row["name"] == ref_data.location
    )
    quantity_unit_id = next(
        row["id"] for row in await call_tool(mcp_client, "quantity_units_list", {}) if row["name"] == ref_data.qu
    )
    product_group_id = next(
        row["id"] for row in await call_tool(mcp_client, "product_groups_list", {}) if row["name"] == ref_data.group
    )

    edit = await call_tool(
        mcp_client,
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
    assert edit[0]["kind"] == "ok", edit

    products = await call_tool(mcp_client, "products_list", {"detail": "full"})
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

    noop = await call_tool(mcp_client, "products_edit", {"items": [{"product": product_id}]})
    assert noop == [{"kind": "ok", "object_id": product_id}]

    unrelated_edit = await call_tool(
        mcp_client,
        "products_edit",
        {"items": [{"product": product_id, "name": f"Edited-{ref_data.suffix}", "description": None}]},
    )
    assert unrelated_edit[0]["kind"] == "ok", unrelated_edit

    products = await call_tool(mcp_client, "products_list", {"detail": "full"})
    product = next(row for row in products if int(row["id"]) == product_id)
    assert product["description"] == "Edited product description"
    assert float(product["min_stock_amount"]) == 0
    assert int(product["product_group_id"]) == int(product_group_id)

    empty_description = await call_tool(
        mcp_client, "products_edit", {"items": [{"product": product_id, "description": ""}]}
    )
    assert empty_description[0]["kind"] == "ok", empty_description
    products = await call_tool(mcp_client, "products_list", {"detail": "full"})
    product = next(row for row in products if int(row["id"]) == product_id)
    # Grocy normalizes an empty product description to null on read.
    assert product["description"] is None

    clear = await call_tool(
        mcp_client,
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
    assert clear[0]["kind"] == "ok", clear

    products = await call_tool(mcp_client, "products_list", {"detail": "full"})
    product = next(row for row in products if int(row["id"]) == product_id)
    assert product["description"] is None
    assert product["product_group_id"] is None
    assert float(product["min_stock_amount"]) == 0
    assert int(product["due_type"]) == 2


async def test_entity_update_advertises_product_columns(mcp_client: Client, ref_data: RefData) -> None:
    tool = next(tool for tool in await mcp_client.list_tools() if tool.name == "entity_update")
    variants = tool.input_schema["properties"]["body"]["anyOf"]
    product_schema = next(schema["properties"] for schema in variants if "qu_id_stock" in schema["properties"])
    rows = await call_tool(
        mcp_client, "entities_get", {"entity_type": "products", "object_ids": [ref_data.product_ids[0]]}
    )
    # The object API returns the product's columns and attaches userfields separately.
    editable = rows[0]["data"].keys() - {"id", "row_created_timestamp", "userfields"}
    assert editable <= product_schema.keys(), editable - product_schema.keys()


async def test_entity_update_patch_semantics(mcp_client: Client, ref_data: RefData) -> None:
    product_id = ref_data.product_ids[0]

    async def product() -> dict:
        rows = await call_tool(mcp_client, "entities_get", {"entity_type": "products", "object_ids": [product_id]})
        data = rows[0]["data"]
        assert isinstance(data, dict)
        return data

    target = {"entity": "products", "objectId": product_id}
    before = await product()
    changes = {
        "description": "New label",
        "quick_open_amount": 2.5,
        "disable_open": 1,
        "default_purchase_price_type": 3,
    }
    await mcp_client.call_tool("entity_update", {**target, "body": changes})
    after = await product()
    assert after["description"] == "New label"
    assert float(after["quick_open_amount"]) == 2.5
    assert int(after["disable_open"]) == 1
    assert int(after["default_purchase_price_type"]) == 3
    assert after["min_stock_amount"] == before["min_stock_amount"]

    rejected = await mcp_client.call_tool("entity_update", {**target, "body": after}, raise_on_error=False)
    assert rejected.is_error
    await mcp_client.call_tool("entity_update", {**target, "body": {"description": None}})
    cleared = await product()
    assert cleared["description"] in (None, "")
    assert cleared["name"] == before["name"]
    assert cleared["disable_open"] == after["disable_open"]


async def test_stock_get_reports_each_physical_location(mcp_client: Client, ref_data: RefData) -> None:
    product, other = ref_data.products
    pantry = ref_data.location
    fridge = f"Fridge-{ref_data.suffix}"
    [location] = await call_tool(mcp_client, "locations_create", {"items": [{"name": fridge}]})
    fridge_id = location["created_object_id"]
    [variant] = await call_tool(mcp_client, "products_edit", {"items": [{"product": other, "parent_product": product}]})
    assert variant["kind"] == "ok", variant
    for name, place, amount, due in (
        (product, pantry, 2, "2030-03-05"),
        (product, fridge, 1, "2030-04-10"),
        (product, fridge, 2, "2030-04-02"),
        (other, fridge, 4, "2031-01-01"),
    ):
        [purchase] = await call_tool(
            mcp_client,
            "stock_add",
            {
                "items": [
                    {"product": name, "qu": ref_data.qu, "location": place, "amount": amount, "best_before_date": due}
                ]
            },
        )
        assert purchase["kind"] == "ok", purchase
        if due == "2030-04-02":
            [edit] = await call_tool(
                mcp_client, "stock_entry_edit", {"items": [{"entry_id": purchase["entry_id"], "open": True}]}
            )
            assert edit["kind"] == "ok", edit

    rows = await call_tool(mcp_client, "stock_get", {"products": [product]})
    assert len(rows) == 2
    assert {(r["location_name"], r["amount"], r["amount_opened"], r["best_before_date"]) for r in rows} == {
        (pantry, 2, 0, "2030-03-05"),
        (fridge, 3, 2, "2030-04-02"),
    }
    [filtered] = await call_tool(
        mcp_client, "stock_get", {"products": [ref_data.product_ids[0]], "locations": [fridge_id]}
    )
    assert filtered == next(row for row in rows if row["location_name"] == fridge)
    at_fridge = await call_tool(mcp_client, "stock_get", {"locations": [fridge]})
    assert {(row["product_name"], row["amount"]) for row in at_fridge} == {(product, 3), (other, 4)}

    await mcp_client.call_tool(
        "entity_update", {"entity": "products", "objectId": ref_data.product_ids[1], "body": {"active": 0}}
    )
    assert await call_tool(mcp_client, "stock_get", {"products": [other]}) == []


async def _create_pack_unit(http: GrocyClient, product_id: int, name: str, factor: int) -> int:
    unit = await http.post("/objects/quantity_units", json={"name": name, "name_plural": f"{name}s"})
    unit.raise_for_status()
    unit_id = int(unit.json()["created_object_id"])
    product = await http.get(f"/objects/products/{product_id}")
    product.raise_for_status()
    conversion = await http.post(
        "/objects/quantity_unit_conversions",
        json={
            "product_id": product_id,
            "from_qu_id": unit_id,
            "to_qu_id": product.json()["qu_id_stock"],
            "factor": factor,
        },
    )
    conversion.raise_for_status()
    return unit_id


async def test_stock_operations(mcp_client: Client, grocy_http_client: GrocyClient, ref_data: RefData) -> None:
    """Add, consume, set stock; verify amounts via stock_get and stock_entries."""
    product = ref_data.products[0]
    qu = ref_data.qu
    loc = ref_data.location

    # Each pack contains one stock unit. Journal amounts are still in stock units.
    pack = f"SinglePack-{ref_data.suffix}"
    pack_id = await _create_pack_unit(grocy_http_client, ref_data.product_ids[0], pack, factor=1)
    ops = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 5, "qu": pack_id, "location": loc}]}
    )
    assert ops[0]["kind"] == "ok", ops
    assert (ops[0]["amount_delta"], ops[0]["new_amount"]) == (5, 5)
    assert (ops[0]["qu_name"], ops[0]["stock_qu_name"]) == (pack, qu)
    assert ops[0]["location_name"] == loc

    # Get stock with product filter
    stock = await call_tool(mcp_client, "stock_get", {"products": [product]})
    our_stock = [s for s in stock if s["product_name"] == product]
    assert len(our_stock) == 1
    assert float(our_stock[0]["amount"]) == 5.0

    # Consume stock
    ops = await call_tool(
        mcp_client, "stock_consume", {"items": [{"product": product, "amount": 2, "qu": qu, "location": loc}]}
    )
    assert ops[0]["kind"] == "ok"
    assert ops[0]["new_amount"] == 3.0

    # Set absolute stock amount
    ops = await call_tool(
        mcp_client, "stock_set", {"items": [{"product": product, "new_amount": 10, "qu": qu, "location": loc}]}
    )
    assert ops[0]["kind"] == "ok"
    assert ops[0]["new_amount"] == 10.0

    # Stock entries by product name
    entries = await call_tool(mcp_client, "stock_entries_list", {"products": [product]})
    assert len(entries) > 0
    assert entries[0]["kind"] == "ok"
    detail = entries[0]["entry"]
    assert detail["product_name"] == product
    entry_id = detail["entry_id"]

    # Edit stock entry — change price, verify diff (batch API)
    edit_results = await call_tool(mcp_client, "stock_entry_edit", {"items": [{"entry_id": entry_id, "price": 9.99}]})
    assert len(edit_results) == 1
    edit_result = edit_results[0]
    assert edit_result["kind"] == "ok"
    assert float(edit_result["entry"]["price"]) == 9.99
    assert edit_result.get("changes") is not None
    assert "price" in edit_result["changes"]

    # Verify edit landed via re-fetch
    entries = await call_tool(mcp_client, "stock_entries_list", {"entry_ids": [entry_id]})
    assert float(entries[0]["entry"]["price"]) == 9.99


async def test_edit_compacting_two_lots_reports_success(
    mcp_client: Client, grocy_http_client: GrocyClient, ref_data: RefData
) -> None:
    product_id = ref_data.product_ids[0]
    path = f"/stock/products/{product_id}"
    for amount, price in [(2, 1), (3, 2)]:
        purchase = await grocy_http_client.post(
            f"{path}/add", json={"amount": amount, "price": price, "best_before_date": "2035-01-31"}
        )
        purchase.raise_for_status()
    lots = await grocy_http_client.get(f"{path}/entries")
    lots.raise_for_status()
    edited_id, survivor = sorted(row["id"] for row in lots.json())

    receipt = await call_tool(mcp_client, "stock_entry_edit", {"items": [{"entry_id": edited_id, "price": 2}]})

    after = await grocy_http_client.get(f"{path}/entries")
    after.raise_for_status()
    assert [(row["id"], row["amount"], row["price"]) for row in after.json()] == [(survivor, 5, 2)]
    [saved] = receipt
    assert saved["kind"] == "ok", saved
    assert saved["entry_id"] == edited_id
    assert saved.get("entry") is None
    assert saved["changes"] == {"price": {"old": 1, "new": 2}}
    journal = await grocy_http_client.get(f"/stock/transactions/{saved['transaction_id']}")
    journal.raise_for_status()
    assert {(row["transaction_type"], row["stock_row_id"]) for row in journal.json()} == {
        ("stock-edit-old", edited_id),
        ("stock-edit-new", edited_id),
    }


async def test_editing_stock_price_preserves_purchase_store(
    mcp_client: Client, grocy_http_client: GrocyClient, ref_data: RefData
) -> None:
    stores = await call_tool(
        mcp_client,
        "entities_create",
        {"items": [{"entity_type": "shopping_locations", "body": {"name": ref_data.suffix}}]},
    )
    store_id = stores[0]["created_object_id"]
    purchase = await grocy_http_client.post(
        f"/stock/products/{ref_data.product_ids[0]}/add", json={"amount": 4, "shopping_location_id": store_id}
    )
    purchase.raise_for_status()
    entries = await call_tool(mcp_client, "stock_entries_list", {"products": [ref_data.product_ids[0]]})
    entry_id = entries[0]["entry"]["entry_id"]

    receipt = await call_tool(mcp_client, "stock_entry_edit", {"items": [{"entry_id": entry_id, "price": 8.5}]})
    assert receipt[0]["kind"] == "ok", receipt
    stored = await grocy_http_client.get(f"/stock/entry/{entry_id}")
    stored.raise_for_status()
    row = stored.json()
    assert row["price"] == 8.5
    assert row["shopping_location_id"] == store_id


async def test_stock_entry_edit_serializes_partial_updates(mcp_client: Client, ref_data: RefData) -> None:
    """Partial edits preserve stored values, serialize dates, and apply explicit clears last."""
    product = ref_data.products[0]
    location = ref_data.location
    locations = await call_tool(mcp_client, "locations_list", {"detail": "full"})
    location_id = next(row["id"] for row in locations if row["name"] == location)
    alternate_location = f"TestStockEdit-{ref_data.suffix}"
    alternate_result = await call_tool(mcp_client, "locations_create", {"items": [{"name": alternate_location}]})
    assert alternate_result[0]["kind"] == "ok", alternate_result
    alternate_location_id = alternate_result[0]["created_object_id"]

    added = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 4, "qu": ref_data.qu, "location": location}]}
    )
    assert added[0]["kind"] == "ok", added
    entry_id = added[0]["entry_id"]

    seeded = (
        await call_tool(
            mcp_client,
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
    unchanged = (
        await call_tool(
            mcp_client,
            "stock_entry_edit",
            {"items": [{"entry_id": entry_id, "price": None, "purchased_date": None, "note": None}]},
        )
    )[0]
    assert unchanged["kind"] == "ok", unchanged
    assert unchanged.get("changes") is None
    assert unchanged["entry"] == seeded_entry

    updated = (
        await call_tool(
            mcp_client,
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

    cleared = (
        await call_tool(
            mcp_client,
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
    seed = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 7, "qu": qu, "location": loc}]}
    )
    assert seed[0]["kind"] == "ok", seed

    # (1) Zero out without passing `qu` — succeeds, falls back to stock QU.
    zero_ops = await call_tool(
        mcp_client, "stock_set", {"items": [{"product": product, "new_amount": 0, "location": loc}]}
    )
    assert zero_ops[0]["kind"] == "ok", zero_ops
    assert zero_ops[0]["new_amount"] == 0.0
    # Fallback uses the product's own stock QU, which is the fixture's QU.
    assert zero_ops[0]["qu_name"] == qu

    # (2) Nonzero without `qu` — batch tools collect per-item errors rather
    # than raising, so the batch succeeds but this item reports an error.
    nonzero_ops = await call_tool(
        mcp_client, "stock_set", {"items": [{"product": product, "new_amount": 3, "location": loc}]}
    )
    assert nonzero_ops[0]["kind"] == "error", nonzero_ops

    # (3) Nonzero with `qu` — still works (regression check for the happy path).
    set_ops = await call_tool(
        mcp_client, "stock_set", {"items": [{"product": product, "new_amount": 3, "qu": qu, "location": loc}]}
    )
    assert set_ops[0]["kind"] == "ok", set_ops
    assert set_ops[0]["new_amount"] == 3.0


async def test_stock_add_returns_entry_id(mcp_client: Client, ref_data: RefData) -> None:
    """`stock_add` echoes the new stock entry's `entry_id`, usable without a follow-up list call."""
    product = ref_data.products[0]
    qu = ref_data.qu
    loc = ref_data.location

    ops = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": loc}]}
    )
    assert ops[0]["kind"] == "ok", ops
    entry_id = ops[0]["entry_id"]
    assert entry_id is not None, ops

    entries = await call_tool(mcp_client, "stock_entries_list", {"entry_ids": [entry_id]})
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
        entries = await call_tool(mcp_client, "stock_entries_list", {"products": [product]})
        latest = max((e["entry"] for e in entries if e["kind"] == "ok"), key=lambda e: e["entry_id"])
        return str(latest["best_before_date"])

    # Case 1: non-freezer, default_best_before_days=300 → today + 300.
    edit = await call_tool(
        mcp_client, "products_edit", {"items": [{"product": product, "default_best_before_days": 300}]}
    )
    assert edit[0]["kind"] == "ok", edit
    ops = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": non_freezer_loc}]}
    )
    assert ops[0]["kind"] == "ok", ops
    expected_case1 = (date.today() + timedelta(days=300)).isoformat()
    assert ops[0]["best_before_date"] == expected_case1
    assert await _latest_bbd() == expected_case1

    # Case 2: non-freezer, default_best_before_days=-1 → 2999-12-31.
    edit = await call_tool(
        mcp_client, "products_edit", {"items": [{"product": product, "default_best_before_days": -1}]}
    )
    assert edit[0]["kind"] == "ok", edit
    ops = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": non_freezer_loc}]}
    )
    assert ops[0]["kind"] == "ok", ops
    assert ops[0]["best_before_date"] == "2999-12-31"
    assert await _latest_bbd() == "2999-12-31"

    # Case 3 (regression for the reported bug): freezer location,
    # default_best_before_days=300 and default_best_before_days_after_freezing=0
    # (the schema default). Without the client-side fallback Grocy would
    # return BBD=today; our tool must return today+300.
    freezer_name = f"TestFreezer-{ref_data.suffix}"
    freezer_create = await call_tool(
        mcp_client, "locations_create", {"items": [{"name": freezer_name, "is_freezer": True}]}
    )
    assert freezer_create[0]["kind"] == "ok", freezer_create
    edit = await call_tool(
        mcp_client, "products_edit", {"items": [{"product": product, "default_best_before_days": 300}]}
    )
    assert edit[0]["kind"] == "ok", edit
    ops = await call_tool(
        mcp_client, "stock_add", {"items": [{"product": product, "amount": 1, "qu": qu, "location": freezer_name}]}
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
    ops = await call_tool(
        mcp_client,
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
    assert ops[0]["kind"] == "ok", ops
    assert ops[0]["best_before_date"] == override.isoformat()
    assert await _latest_bbd() == override.isoformat()


async def test_shopping_list_operations(mcp_client: Client, ref_data: RefData, grocy_http_client: GrocyClient) -> None:
    """Add items (product-linked, note-only, product+note), get list, edit item, remove item."""
    product = ref_data.products[0]

    # Add stock first so the product exists with stock
    await call_tool(
        mcp_client,
        "stock_add",
        {"items": [{"product": product, "amount": 1, "qu": ref_data.qu, "location": ref_data.location}]},
    )

    # Add items to default shopping list
    sl_results = await call_tool(
        mcp_client,
        "shopping_list_items_add",
        {
            "items": [
                {"product": product, "amount": 2, "shopping_list": 1},
                {"note": "Check paper towels", "shopping_list": 1},
                {"product": product, "amount": 1, "note": "get the organic brand", "shopping_list": 1},
            ]
        },
    )
    assert all(r["kind"] == "ok" for r in sl_results)
    sl_item_id = sl_results[0]["item_id"]

    # Choose a shopping unit that contains six stock units, as a user can in Grocy.
    case_id = await _create_pack_unit(grocy_http_client, ref_data.product_ids[0], f"Case-{ref_data.suffix}", factor=6)
    updated = await grocy_http_client.put(f"/objects/shopping_list/{sl_item_id}", json={"qu_id": case_id, "amount": 12})
    updated.raise_for_status()

    # Get shopping list — verify items present
    sl_data = await call_tool(mcp_client, "shopping_list_get", {"shopping_list": 1})
    assert "items" in sl_data
    our_items = [i for i in sl_data["items"] if i.get("product_name") == product]
    assert len(our_items) >= 2
    product_plus_note = [i for i in our_items if i.get("note") == "get the organic brand"]
    assert len(product_plus_note) == 1

    exact = await call_tool(mcp_client, "entities_get", {"entity_type": "shopping_list", "object_ids": [sl_item_id]})
    assert len(exact) == 1
    assert exact[0]["kind"] == "ok"
    assert exact[0]["entity_type"] == "shopping_list"
    assert exact[0]["object_id"] == sl_item_id
    assert int(exact[0]["data"]["id"]) == sl_item_id
    assert int(exact[0]["data"]["qu_id"]) == case_id
    assert float(exact[0]["data"]["amount"]) == 12
    assert exact[0]["shopping_item_names"]["unit_name"] == ref_data.qu
    listed = next(item for item in our_items if item["item_id"] == sl_item_id)
    assert (listed["amount"], listed["qu_name"]) == (12, ref_data.qu)
    assert exact[0]["shopping_item_names"]["product_name"] == product
    assert exact[0]["shopping_item_names"]["list_name"] == sl_data["name"]

    # Edit item — mark as done
    edit_sl = await call_tool(mcp_client, "shopping_list_item_edit", {"item_id": sl_item_id, "done": True})
    assert (edit_sl["kind"], edit_sl["amount"], edit_sl["qu_name"]) == ("ok", 12, ref_data.qu)
    edited = await call_tool(mcp_client, "entities_get", {"entity_type": "shopping_list", "object_ids": [sl_item_id]})
    assert int(edited[0]["data"]["done"]) == 1
    assert int(edited[0]["data"]["qu_id"]) == case_id

    # Remove item
    await call_tool(mcp_client, "shopping_list_items_remove", {"item_ids": [sl_item_id]})


async def test_clear_shopping_list_keeps_list_and_other_lists_items(
    mcp_client: Client, grocy_http_client: GrocyClient
) -> None:
    list_name = f"Clear-{uuid.uuid4().hex[:8]}"
    [created] = await call_tool(mcp_client, "shopping_lists_create", {"items": [{"name": list_name}]})
    list_id = created["created_object_id"]
    for done in (0, 1):
        response = await grocy_http_client.post(
            "/objects/shopping_list", json={"shopping_list_id": list_id, "note": f"Done={done}", "done": done}
        )
        response.raise_for_status()
    control = await grocy_http_client.post("/objects/shopping_list", json={"shopping_list_id": 1, "note": list_name})
    control.raise_for_status()
    control_id = control.json()["created_object_id"]

    for ref in (list_name.lower(), list_id):
        assert await call_tool(mcp_client, "shopping_list_clear", {"shopping_list": ref}) == {"kind": "ok"}
        kept = await call_tool(mcp_client, "shopping_list_get", {"shopping_list": list_id})
        assert kept["name"] == list_name
        assert kept["items"] == []

    untouched = await grocy_http_client.get(f"/objects/shopping_list/{control_id}")
    untouched.raise_for_status()
    assert untouched.json()["note"] == list_name


async def _add_dated_stock(client: Client, ref_data: RefData, products: dict[str, date]) -> None:
    items = [
        {
            "product": name,
            "qu": ref_data.qu,
            "location": ref_data.location,
            "amount": 3,
            "best_before_date": due.isoformat(),
        }
        for name, due in products.items()
    ]
    added = await call_tool(client, "stock_add", {"items": items})
    assert [row["kind"] for row in added] == ["ok"] * len(items), added


async def test_expiring_stock_returns_openable_lots_with_their_own_amounts_and_locations(
    mcp_client: Client, ref_data: RefData
) -> None:
    today = datetime.now(UTC).date()
    product = ref_data.products[0]
    fridge = f"Fridge-{ref_data.suffix}"
    [location] = await call_tool(mcp_client, "locations_create", {"items": [{"name": fridge}]})
    assert location["kind"] == "ok", location
    matching = {}
    for place, amount, days, price in [(fridge, 2, 1, 1), (fridge, 3, 1, 2), (ref_data.location, 5, 20, 1)]:
        [added] = await call_tool(
            mcp_client,
            "stock_add",
            {
                "items": [
                    {
                        "product": product,
                        "location": place,
                        "amount": amount,
                        "price": price,
                        "qu": ref_data.qu,
                        "best_before_date": (today + timedelta(days=days)).isoformat(),
                    }
                ]
            },
        )
        assert added["kind"] == "ok", added
        if days == 1:
            matching[added["entry_id"]] = amount

    rows = await call_tool(mcp_client, "get_expiring_stock", {"days_ahead": 3})
    lots = [r for r in rows if r["product_name"] == product]
    assert len(lots) == len(matching) == 2
    due = (today + timedelta(days=1)).isoformat()
    assert {(r["entry_id"], r["location_name"], r["amount"], r["best_before_date"]) for r in lots} == {
        (entry_id, fridge, amount, due) for entry_id, amount in matching.items()
    }
    details = await call_tool(mcp_client, "stock_entries_list", {"entry_ids": list(matching)})
    assert {r["entry"]["entry_id"]: r["entry"]["amount"] for r in details} == matching


async def test_expiring_stock_honors_window(mcp_client: Client, ref_data: RefData) -> None:
    today = datetime.now(UTC).date()
    soon, later = ref_data.products
    await _add_dated_stock(mcp_client, ref_data, {soon: today + timedelta(days=2), later: today + timedelta(days=12)})

    for window, expected in [(0, {}), (11, {soon: 2}), (12, {soon: 2, later: 12})]:
        rows = await call_tool(mcp_client, "get_expiring_stock", {"days_ahead": window})
        assert {
            row["product_name"]: row["days_until_expiry"] for row in rows if row["product_name"] in ref_data.products
        } == expected

    await _add_dated_stock(mcp_client, ref_data, {soon: today})
    due_today = await call_tool(mcp_client, "get_expiring_stock", {"days_ahead": 0})
    assert [row["days_until_expiry"] for row in due_today if row["product_name"] == soon] == [0]


async def test_expired_stock_distinguishes_use_by_from_best_before(mcp_client: Client, ref_data: RefData) -> None:
    best_before, use_by = ref_data.products
    edited = await call_tool(mcp_client, "products_edit", {"items": [{"product": use_by, "due_type": 2}]})
    assert edited[0]["kind"] == "ok", edited

    past = datetime.now(UTC).date() - timedelta(days=2)
    await _add_dated_stock(mcp_client, ref_data, {best_before: past, use_by: past})
    rows = await call_tool(mcp_client, "get_expired_stock", {})
    assert best_before not in {row["product_name"] for row in rows}
    [lot] = await call_tool(mcp_client, "stock_entries_list", {"products": [use_by]})
    assert [row for row in rows if row["product_name"] == use_by] == [
        {
            "product_id": ref_data.product_ids[1],
            "entry_id": lot["entry"]["entry_id"],
            "product_name": use_by,
            "amount": 3,
            "qu_name": ref_data.qu,
            "location_name": ref_data.location,
            "best_before_date": past.isoformat(),
            "days_overdue": 2,
        }
    ]


async def test_stock_status_queries(mcp_client: Client) -> None:
    await call_tool(mcp_client, "get_below_minimum_stock", {})
    await call_tool(mcp_client, "get_db_changed_time", {})


async def test_generic_entity_crud(mcp_client: Client, ref_data: RefData) -> None:
    """entities_create, entities_get, entities_list with read-only types."""
    suffix = uuid.uuid4().hex[:6]

    # Create via generic path
    pg_create = await call_tool(
        mcp_client,
        "entities_create",
        {"items": [{"entity_type": "product_groups", "body": {"name": f"TestGroupGeneric-{suffix}"}}]},
    )
    assert pg_create[0]["kind"] == "ok"
    pg_id = pg_create[0]["created_object_id"]

    # Fetch by ID
    pg_fetched = await call_tool(mcp_client, "entities_get", {"entity_type": "product_groups", "object_ids": [pg_id]})
    assert pg_fetched[0]["kind"] == "ok"
    assert pg_fetched[0]["data"]["name"] == f"TestGroupGeneric-{suffix}"

    # Read-only entity types accepted by entities_list (the broader ``ReadableEntityType`` set)
    view_only = ["stock", "products_last_purchased", "permission_hierarchy"]
    rows = await call_tool(mcp_client, "entities_list", {"entity_types": view_only})
    assert set(view_only) <= set(rows)

    # Fetch product via generic path
    entities = await call_tool(
        mcp_client, "entities_get", {"entity_type": "products", "object_ids": [ref_data.product_ids[0]]}
    )
    assert entities[0]["kind"] == "ok"
    assert entities[0]["data"]["name"] == ref_data.products[0]


async def test_create_entities_rejects_view_only_type(mcp_client: Client) -> None:
    """``entities_create`` only accepts ``WriteableEntityType``."""
    with pytest.raises(Exception, match="entity_type"):
        await mcp_client.call_tool("entities_create", {"items": [{"entity_type": "stock", "body": {"amount": 1}}]})


if __name__ == "__main__":
    pytest_bazel.main()
