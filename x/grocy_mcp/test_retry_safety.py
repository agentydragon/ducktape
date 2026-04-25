"""Unit tests for retry safety and QU validation in batch_tools.

These tests mock the httpx client via respx to verify:
- Mutating POSTs are never re-executed after they succeed (even when follow-up GET fails)
- Legitimate retries work when the POST itself fails transiently
- QU validation rejects mismatched units
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from x.grocy_mcp.batch_tools import register_batch_tools
from x.grocy_mcp.mcp_types import ServerSettings

BASE_URL = "https://grocy.example.com/api"

PRODUCTS = [{"id": 1, "name": "TestProduct", "qu_id_stock": 1, "location_id": 1}]
LOCATIONS = [{"id": 1, "name": "TestLoc"}]
QUS = [{"id": 1, "name": "pieces"}, {"id": 2, "name": "grams"}]
CONVERSIONS: list[dict[str, object]] = []  # no conversions in these tests

ADD_RESPONSE = [{"transaction_id": "tx-123", "amount": 5}]
STOCK_RESPONSE = {"stock_amount": 5}


def _settings() -> ServerSettings:
    return ServerSettings(grocy_url=BASE_URL.removesuffix("/api"), max_retries=2, retry_base_delay=0.01)


def _setup_resolver_routes(router: respx.Router) -> None:
    """Register routes the EntityResolver needs."""
    router.get("/objects/products").respond(json=PRODUCTS)
    router.get("/objects/locations").respond(json=LOCATIONS)
    router.get("/objects/quantity_units").respond(json=QUS)
    router.get("/objects/quantity_unit_conversions_resolved").respond(json=CONVERSIONS)


@pytest.fixture
async def mcp_client() -> AsyncGenerator[tuple[Client, respx.Router]]:
    """MCP client + respx router for configuring per-test responses."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        _setup_resolver_routes(router)
        http_client = httpx.AsyncClient(base_url=BASE_URL)
        mcp = FastMCP("test")
        register_batch_tools(mcp, http_client, _settings())
        async with Client(FastMCPTransport(mcp)) as client:
            yield client, router


async def test_add_stock_post_not_retried_when_get_fails(mcp_client: tuple[Client, respx.Router]) -> None:
    """POST succeeds on first try, GET for new_amount fails -> POST must NOT be retried."""
    client, router = mcp_client

    post_route = router.post("/stock/products/1/add").respond(json=ADD_RESPONSE)
    # GET for new_amount fails
    router.get("/stock/products/1").respond(500)

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "ok", f"expected ok, got: {op}"
    assert op["new_amount"] is None  # GET failed
    assert op["qu_name"] == "pieces"
    assert op["location_name"] == "TestLoc"
    assert post_route.call_count == 1


async def test_add_stock_post_retried_on_transient_failure(mcp_client: tuple[Client, respx.Router]) -> None:
    """POST fails with 500 then succeeds -> POST should be retried (legitimate)."""
    client, router = mcp_client

    post_route = router.post("/stock/products/1/add").mock(
        side_effect=[httpx.Response(500, json={}), httpx.Response(200, json=ADD_RESPONSE)]
    )
    router.get("/stock/products/1").respond(json=STOCK_RESPONSE)

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "ok", f"expected ok, got: {op}"
    assert op["new_amount"] == 5.0
    assert post_route.call_count == 2  # first attempt failed, second succeeded


async def test_unit_validation_rejects_wrong_qu(mcp_client: tuple[Client, respx.Router]) -> None:
    """Specifying a QU with no conversion to stock QU should fail validation."""
    client, _router = mcp_client

    result = await client.call_tool(
        "stock_add", {"items": [{"product": 1, "amount": 5, "qu": "grams", "location": "TestLoc"}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "error", f"expected error for wrong QU, got: {op}"
    assert "pieces" in op["error"], "error should mention the expected stock QU"


async def test_unit_validation_rejects_missing_qu(mcp_client: tuple[Client, respx.Router]) -> None:
    """Omitting qu should fail validation."""
    client, _router = mcp_client
    with pytest.raises(Exception, match="qu"):
        await client.call_tool("stock_add", {"items": [{"product": 1, "amount": 5, "location": "TestLoc"}]})


async def test_http_errors_are_compact_and_include_status_url_and_body(mcp_client: tuple[Client, respx.Router]) -> None:
    """HTTP backend errors should be concise and actionable (no Python traceback)."""
    client, router = mcp_client
    error_payload = {"error_message": "Amount to be consumed cannot be > current stock amount"}
    router.post("/stock/products/1/consume").respond(status_code=400, json=error_payload)

    result = await client.call_tool(
        "stock_consume", {"items": [{"product": 1, "amount": 5, "qu": "pieces", "location": "TestLoc"}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["kind"] == "error", f"expected error, got: {op}"
    expected_body = httpx.Response(status_code=400, json=error_payload).text
    assert op["error"] == (
        "HTTP 400 Bad Request for POST https://grocy.example.com/api/stock/products/1/consume\n"
        f"Grocy response body: {expected_body}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
