"""Unit tests for backend failures and QU validation in batch_tools.

These tests mock the httpx2 client via pytest-httpx2's RESPX router to verify:
- Mutating POSTs are never re-executed after they succeed (even when follow-up GET fails)
- Legitimate retries work when the POST itself fails transiently
- QU validation rejects mismatched units
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import date, timedelta

import httpx
import httpx2
import pytest
import pytest_bazel
import respx
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from grocy_mcp.batch_tools import build_batch_tools_mcp
from grocy_mcp.client import GrocyClient
from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.request_scoped_openapi import borrowed_http_client_provider
from grocy_mcp.testing.results import unwrap_result

BASE_URL = "https://grocy.example.com/api"
# Not every test calls every route _setup_entity_routes registers.
pytestmark = pytest.mark.httpx2(base_url=BASE_URL, assert_all_called=False)

PRODUCTS = [{"id": 1, "name": "TestProduct", "qu_id_stock": 1, "location_id": 1}]
LOCATIONS = [{"id": 1, "name": "TestLoc"}]
QUS = [{"id": 1, "name": "pieces"}, {"id": 2, "name": "grams"}]
CONVERSIONS: list[dict[str, object]] = []  # no conversions in these tests

ADD_RESPONSE = [{"transaction_id": "tx-123", "amount": 5}]


def _settings() -> ServerSettings:
    return ServerSettings(grocy_url=BASE_URL.removesuffix("/api"), max_retries=2, retry_base_delay=0.01)


def _setup_entity_routes(router: respx.Router) -> None:
    """Register routes the Grocy client's entity methods need."""
    router.get("/objects/products").respond(json=PRODUCTS)
    router.get("/objects/locations").respond(json=LOCATIONS)
    router.get("/objects/quantity_units").respond(json=QUS)
    router.get("/objects/quantity_unit_conversions_resolved").respond(json=CONVERSIONS)


@pytest.fixture
async def mcp_client(httpx2_mock: respx.Router) -> AsyncGenerator[tuple[Client, respx.Router]]:
    """MCP client + respx router for configuring per-test responses."""
    router = httpx2_mock
    _setup_entity_routes(router)
    async with GrocyClient(base_url=BASE_URL) as http_client:
        mcp = build_batch_tools_mcp(_settings(), client_provider=borrowed_http_client_provider(http_client))
        async with Client(FastMCPTransport(mcp)) as client:
            yield client, router


async def test_add_stock_post_not_retried_when_get_fails(mcp_client: tuple[Client, respx.Router]) -> None:
    """POST succeeds on first try, GET for new_amount fails -> POST must NOT be retried."""
    client, router = mcp_client

    post_route = router.post("/stock/products/1/add").respond(json=ADD_RESPONSE)
    # GET for new_amount fails
    router.get("/stock/products/1").respond(status_code=500)

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    op = unwrap_result(result)[0]
    assert op["kind"] == "ok", f"expected ok, got: {op}"
    assert op["new_amount"] is None  # GET failed
    assert op["qu_name"] == "pieces"
    assert op["location_name"] == "TestLoc"
    assert post_route.calls.call_count == 1


async def test_incomplete_location_fails_before_stock_write(mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    router.get("/objects/locations").respond(json=[*LOCATIONS, {"id": 2}])
    item = {"product": 1, "amount": 3, "qu": 1}
    add = router.post("/stock/products/1/add").respond(json=ADD_RESPONSE)
    transfer = router.post("/stock/products/1/transfer").respond(json=ADD_RESPONSE)

    [added] = unwrap_result(await client.call_tool("stock_add", {"items": [{**item, "location": 2}]}))
    moved = unwrap_result(await client.call_tool("stock_transfer", {**item, "from_location": 1, "to_location": 2}))

    assert added["kind"] == moved["kind"] == "error"
    assert add.calls.call_count == transfer.calls.call_count == 0


async def test_add_stock_post_not_retried_on_server_error(mcp_client: tuple[Client, respx.Router]) -> None:
    """A 500 may follow an applied mutation, so surface it without resending."""
    client, router = mcp_client

    error_body = {"error_message": "backend failed after starting the mutation"}
    post_route = router.post("/stock/products/1/add").mock(
        # respx's side_effect only recognizes real httpx.Response (see Route.resolve's
        # isinstance check); httpx2.Response looks identical but isn't cloned/bound to
        # the request. The second response would reveal an accidental retry.
        side_effect=[httpx.Response(500, json=error_body), httpx.Response(200, json=ADD_RESPONSE)]
    )

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    op = unwrap_result(result)[0]
    assert op["kind"] == "error", f"expected error, got: {op}"
    assert post_route.calls.call_count == 1, "mutating POST must not be resent after an HTTP 500"
    assert "may or may not have been applied" in op["error"]
    assert "HTTP 500 Internal Server Error" in op["error"]
    assert "Grocy response body: " in op["error"]
    assert error_body["error_message"] in op["error"]


async def test_stock_add_uses_resolved_shelf_life_and_refreshes_on_next_call(
    mcp_client: tuple[Client, respx.Router],
) -> None:
    client, router = mcp_client
    product = {**PRODUCTS[0], "id": "1", "default_best_before_days": 0, "default_best_before_days_after_freezing": -1}
    products = router.get("/objects/products").respond(json=[product])
    locations = router.get("/objects/locations").respond(json=[{**LOCATIONS[0], "id": "1", "is_freezer": "1"}])
    add = router.post("/stock/products/1/add").respond(json=ADD_RESPONSE)
    router.get("/stock/products/1").respond(json={"stock_amount": 5})
    item = {"product": "TestProduct", "qu": 1, "location": 1, "amount": 5}

    for index, frozen_days in enumerate([-1, 4], start=1):
        products.respond(json=[{**product, "default_best_before_days_after_freezing": frozen_days}])
        [saved] = unwrap_result(await client.call_tool("stock_add", {"items": [item]}))
        due = "2999-12-31" if frozen_days == -1 else (date.today() + timedelta(days=frozen_days)).isoformat()
        assert saved["kind"] == "ok", saved
        assert saved["best_before_date"] == due
        assert json.loads(add.calls.last.request.content)["best_before_date"] == due
        assert (products.calls.call_count, locations.calls.call_count) == (index, index)


async def test_one_to_one_transfer_reports_stock_unit(mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    products = router.get("/objects/products").respond(json=PRODUCTS)
    locations = router.get("/objects/locations").respond(json=[*LOCATIONS, {"id": "2", "name": "Fridge"}])
    router.get("/objects/quantity_units").respond(json=[QUS[0], {"id": 3, "name": "single pack"}])
    router.get("/objects/quantity_unit_conversions_resolved").respond(
        json=[{"from_qu_id": 3, "to_qu_id": 1, "product_id": 1, "factor": 1}]
    )
    transfer = router.post("/stock/products/1/transfer").respond(json=ADD_RESPONSE)
    saved = unwrap_result(
        await client.call_tool(
            "stock_transfer",
            {"product": 1, "amount": 3, "qu": "single pack", "from_location": "TestLoc", "to_location": 2},
        )
    )
    assert saved["kind"] == "ok", saved
    assert saved["qu_name"] == "single pack"
    assert saved.get("stock_qu_name") == "pieces"
    body = json.loads(transfer.calls.last.request.content)
    assert (body["location_id_from"], body["location_id_to"], body["amount"]) == (1, 2, 3)
    assert (products.calls.call_count, locations.calls.call_count) == (1, 1)


async def test_stock_edit_lost_reply_reports_uncertain_outcome(mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    path = "/stock/entry/7"
    router.get(path).respond(json={"id": 7, "product_id": 1, "amount": 2, "open": 0})
    write = router.put(path).mock(side_effect=httpx2.ReadTimeout("reply lost"))
    result = await client.call_tool("stock_entry_edit", {"items": [{"entry_id": 7, "price": 3}]})
    receipt = unwrap_result(result)[0]
    assert write.calls.call_count == 1
    assert receipt["kind"] == "error"
    assert receipt["entry_id"] == 7
    assert "may or may not have been applied" in receipt["error"]


@pytest.mark.parametrize("unavailable", ["entry", "names"])
async def test_saved_stock_edit_survives_unavailable_details(
    mcp_client: tuple[Client, respx.Router], unavailable: str
) -> None:
    client, router = mcp_client
    path = "/stock/entry/7"
    current = {"id": 7, "product_id": 1, "amount": 2, "open": 0, "location_id": 1, "price": 1}
    old = {
        **current,
        "price": 2,
        "opened_date": None,
        "best_before_date": None,
        "purchased_date": None,
        "shopping_location_id": None,
        "note": None,
        "transaction_id": "saved-edit",
    }
    # Another writer changed the price between our initial GET and the PUT.
    journal = [dict(old, transaction_type="stock-edit-new", price=3), dict(old, transaction_type="stock-edit-old")]
    write = router.put(path).respond(json=journal)
    after = httpx.Response(503) if unavailable == "entry" else httpx.Response(200, json={**current, "price": 3})
    router.get(path).mock(side_effect=[httpx.Response(200, json=current), after, after, after])
    if unavailable == "names":
        router.get("/objects/products").respond(status_code=503)

    result = await client.call_tool("stock_entry_edit", {"items": [{"entry_id": 7, "price": 3}]})
    [saved] = unwrap_result(result)
    assert write.calls.call_count == 1
    assert saved["kind"] == "ok", saved
    assert saved["transaction_id"] == "saved-edit"
    assert saved["changes"] == {"price": {"old": 2, "new": 3}}
    if unavailable == "entry":
        assert saved.get("entry") is None
    else:
        assert saved["entry"]["entry_id"] == 7
        assert saved["entry"]["price"] == 3


async def test_mutating_post_not_retried_on_timeout(mcp_client: tuple[Client, respx.Router]) -> None:
    """A timeout on a mutating POST must NOT re-send — a re-POST would double-apply it."""
    client, router = mcp_client

    post_route = router.post("/stock/products/1/consume").mock(side_effect=[httpx2.ReadTimeout("simulated timeout")])

    result = await client.call_tool(
        "stock_consume", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    op = unwrap_result(result)[0]
    assert op["kind"] == "error", f"expected error, got: {op}"
    assert post_route.calls.call_count == 1, "mutating POST must be sent exactly once on timeout"
    error = op["error"]
    assert "consume" in error, "error should name the endpoint"
    assert "may or may not" in error, "error should say the mutation may or may not have applied"
    assert "double-appl" in error, "error should warn against a blind retry"


@pytest.mark.parametrize(
    ("tool", "args", "path", "payload", "failure"),
    [
        ("stock_get", {}, "/objects/stock", [], httpx2.ReadTimeout("simulated timeout")),
        ("stock_get", {}, "/objects/stock", [], httpx.Response(503)),
        ("stock_entries_list", {"products": [1]}, "/stock/products/1/entries", [], httpx.Response(503)),
        ("get_below_minimum_stock", {}, "/stock/volatile", {"missing_products": []}, httpx.Response(503)),
    ],
)
async def test_stock_reports_retry_temporary_read_failures(
    mcp_client: tuple[Client, respx.Router],
    tool: str,
    args: dict,
    path: str,
    payload: list | dict,
    failure: httpx2.ReadTimeout | httpx.Response,
) -> None:
    client, router = mcp_client
    route = router.get(path).mock(side_effect=[failure, httpx.Response(200, json=payload)])
    assert unwrap_result(await client.call_tool(tool, args)) == []
    assert route.calls.call_count == 2


async def test_stock_entries_keep_other_products_when_a_name_is_missing(
    mcp_client: tuple[Client, respx.Router],
) -> None:
    client, router = mcp_client
    products = router.get("/objects/products").respond(json=PRODUCTS)
    router.get("/stock/products/1/entries").respond(json=[{"id": 17, "product_id": 1, "amount": 3}])

    rows = unwrap_result(await client.call_tool("stock_entries_list", {"products": ["TestProduct", "Absent", 1]}))
    assert [row["kind"] for row in rows] == ["ok", "error", "ok"]
    assert rows[0]["entry"] == rows[2]["entry"]
    assert rows[0]["entry"]["entry_id"] == 17
    assert "Absent" in rows[1]["error"]
    assert products.calls.call_count == 1

    products.respond(json=[{**PRODUCTS[0], "name": "Renamed product"}])
    [row] = unwrap_result(await client.call_tool("stock_entries_list", {"products": [1]}))
    assert row["entry"]["product_name"] == "Renamed product"
    assert products.calls.call_count == 2


async def test_shopping_list_catalogs_are_read_once_per_report(mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    shopping_list = {"id": 1, "name": "Weekly"}
    router.get("/objects/shopping_lists").respond(json=[shopping_list])
    items = router.get("/objects/shopping_list")
    items.respond(json=[{"id": 10 + i, "product_id": p} for i, p in enumerate(["1", 1, 2, 3, None])])
    products = router.get("/objects/products").respond(json=[*PRODUCTS, {"id": 2, "name": "Sugar", "qu_id_stock": 9}])
    units = router.get("/objects/quantity_units").respond(json=QUS)

    async def names() -> list[tuple[str | None, str | None]]:
        report = unwrap_result(await client.call_tool("shopping_list_get", {"shopping_list": 1}))
        return [(row["product_name"], row["qu_name"]) for row in report["items"]]

    assert await names() == [
        ("TestProduct", "pieces"),
        ("TestProduct", "pieces"),
        ("Sugar", "id=9"),
        ("id=3", None),
        (None, None),
    ]
    assert (products.calls.call_count, units.calls.call_count) == (1, 1)

    products.respond(json=[])
    assert await names() == [("id=1", None), ("id=1", None), ("id=2", None), ("id=3", None), (None, None)]
    assert (products.calls.call_count, units.calls.call_count) == (2, 1)
    items.respond(json=[{"id": 15, "note": "Look for coupons"}])
    assert await names() == [(None, None)]
    assert (products.calls.call_count, units.calls.call_count) == (2, 1)


@pytest.mark.parametrize("tool", ["shopping_list_items_add", "shopping_list_item_edit", "shopping_list_items_remove"])
async def test_shopping_write_survives_unavailable_names(tool: str, mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    path = "/objects/shopping_list/12"
    router.get(path).respond(json={"id": 12, "product_id": "1", "amount": "3", "note": "Back shelf", "done": 0})
    router.get("/objects/products/1").respond(json=PRODUCTS[0])
    router.get("/objects/quantity_units/1").respond(status_code=503)
    if tool == "shopping_list_items_add":
        router.get("/objects/shopping_lists").respond(json=[{"id": 1, "name": "Weekly"}])
        mutation = router.post("/objects/shopping_list").respond(json={"created_object_id": 12})
        arguments: dict[str, object] = {"items": [{"shopping_list": 1, "product": 1, "amount": 3}]}
    elif tool == "shopping_list_item_edit":
        mutation = router.put(path).respond(status_code=204)
        arguments = {"item_id": 12, "done": True}
    else:
        mutation = router.delete(path).respond(status_code=204)
        arguments = {"item_ids": [12]}

    result = unwrap_result(await client.call_tool(tool, arguments))
    receipt = result[0] if isinstance(result, list) else result
    assert mutation.calls.call_count == 1
    if tool == "shopping_list_item_edit":
        # Echoing the earlier snapshot would overwrite another client's note or quantity.
        assert json.loads(mutation.calls.last.request.content) == {"done": 1}
    assert receipt["kind"] == "ok", receipt
    assert (receipt["item_id"], receipt["amount"], receipt["product_name"], receipt["qu_name"]) == (
        12,
        3,
        "TestProduct",
        "id=1",
    )


@pytest.mark.parametrize(
    ("tool", "method", "path", "args"),
    [
        ("products_edit", "PUT", "/objects/products/1", {"items": [{"product": 1, "description": "Updated"}]}),
        ("product_delete", "DELETE", "/objects/products/1", {"product": 1}),
        ("shopping_list_item_edit", "PUT", "/objects/shopping_list/12", {"item_id": 12, "done": True}),
        ("shopping_list_items_remove", "DELETE", "/objects/shopping_list/12", {"item_ids": [12]}),
    ],
)
async def test_object_writes_retry_only_before_delivery(
    mcp_client: tuple[Client, respx.Router], tool: str, method: str, path: str, args: dict
) -> None:
    client, router = mcp_client
    router.get("/objects/shopping_list/12").respond(json={"id": 12, "amount": 1})
    write = router.put(path) if method == "PUT" else router.delete(path)
    write.mock(side_effect=[httpx2.ConnectTimeout("connection not established"), httpx.Response(204)])

    result = unwrap_result(await client.call_tool(tool, args))
    assert write.calls.call_count == 2, result
    assert (result[0] if isinstance(result, list) else result)["kind"] == "ok", result

    write.mock(side_effect=httpx2.ReadTimeout("lost reply"))
    result = unwrap_result(await client.call_tool(tool, args))
    receipt = result[0] if isinstance(result, list) else result
    assert receipt["kind"] == "error", receipt
    assert "may or may not have been applied" in receipt["error"]
    assert write.calls.call_count == 3


async def test_low_stock_report_survives_unavailable_locations(mcp_client: tuple[Client, respx.Router]) -> None:
    client, router = mcp_client
    router.get("/stock/volatile").respond(
        json={
            "missing_products": [{"id": 1, "amount_missing": "2", "product": {**PRODUCTS[0], "min_stock_amount": "5"}}]
        }
    )
    router.get("/objects/locations").respond(status_code=503)

    result = await client.call_tool("get_below_minimum_stock", {})

    assert unwrap_result(result) == [
        {
            "product_id": 1,
            "product_name": "TestProduct",
            "amount": 3,
            "min_amount": 5,
            "qu_name": "pieces",
            "deficit": 2,
        }
    ]


async def test_unit_validation_rejects_wrong_qu(mcp_client: tuple[Client, respx.Router]) -> None:
    """Specifying a QU with no conversion to stock QU should fail validation."""
    client, _router = mcp_client

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "grams", "location": "TestLoc"}]}
    )
    op = unwrap_result(result)[0]
    assert op["kind"] == "error", f"expected error for wrong QU, got: {op}"
    assert "pieces" in op["error"], "error should mention the expected stock QU"


async def test_http_errors_are_compact_and_include_status_url_and_body(mcp_client: tuple[Client, respx.Router]) -> None:
    """HTTP backend errors should be concise and actionable (no Python traceback)."""
    client, router = mcp_client
    error_payload = {"error_message": "Amount to be consumed cannot be > current stock amount"}
    router.post("/stock/products/1/consume").respond(status_code=400, json=error_payload)

    result = await client.call_tool(
        "stock_consume", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    op = unwrap_result(result)[0]
    assert op["kind"] == "error", f"expected error, got: {op}"
    expected_body = httpx2.Response(status_code=400, json=error_payload).text
    assert op["error"] == (
        "HTTP 400 Bad Request for POST https://grocy.example.com/api/stock/products/1/consume\n"
        f"Grocy response body: {expected_body}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
