"""End-to-end tool tests via an in-memory FastMCP client (FakePlaidExtras, no network)."""

from typing import Any

import pytest
import pytest_bazel
from fastmcp.client import Client
from fastmcp.exceptions import ToolError


def unwrap(result: Any) -> Any:
    """Structured content of a CallToolResult, unwrapping FastMCP's {'result': ...} list wrapper."""
    sc = result.structured_content
    assert sc is not None, "no structured_content in CallToolResult"
    if isinstance(sc, dict) and len(sc) == 1 and "result" in sc:
        return sc["result"]
    return sc


async def test_list_items_lists_configured_items(client: Client) -> None:
    items = unwrap(await client.call_tool("list_items", {}))
    assert {i["key"] for i in items} == {"chase", "bofa"}
    chase = next(i for i in items if i["key"] == "chase")
    assert "liabilities" in chase["products"]


async def test_list_accounts_returns_balances(client: Client) -> None:
    accounts = unwrap(await client.call_tool("list_accounts", {"item": "chase"}))
    by_id = {a["account_id"]: a for a in accounts}
    assert set(by_id) == {"acc_cc", "acc_chk"}
    assert by_id["acc_cc"]["balances"]["limit"] == 10000.0


async def test_list_transactions_paginates_within_range(client: Client) -> None:
    page = unwrap(
        await client.call_tool(
            "list_transactions",
            {"item": "chase", "start_date": "2026-05-01", "end_date": "2026-05-31", "offset": 1, "count": 2},
        )
    )
    # total is the full in-range count (5) before offset/count slicing.
    assert page["total"] == 5
    assert [t["transaction_id"] for t in page["transactions"]] == ["txn_1", "txn_2"]
    assert page["transactions"][0]["amount"] == 11.0
    assert page["transactions"][0]["personal_finance_category"]["primary"] == "FOOD_AND_DRINK"


async def test_get_liabilities(client: Client) -> None:
    liabilities = unwrap(await client.call_tool("get_liabilities", {"item": "chase"}))
    # Card-only item: mortgage/student arrays are null.
    assert liabilities["mortgage"] is None
    assert liabilities["student"] is None
    cards = liabilities["credit"]
    assert len(cards) == 1
    assert cards[0]["account_id"] == "acc_cc"
    assert cards[0]["last_statement_balance"] == 1543.21
    assert cards[0]["aprs"][0]["apr_type"] == "purchase_apr"


async def test_liabilities_rejects_item_without_product(client: Client) -> None:
    # bofa is configured without the 'liabilities' product.
    with pytest.raises(ToolError):
        await client.call_tool("get_liabilities", {"item": "bofa"})


async def test_unknown_item_raises(client: Client) -> None:
    with pytest.raises(ToolError):
        await client.call_tool("list_accounts", {"item": "nope"})


if __name__ == "__main__":
    pytest_bazel.main()
