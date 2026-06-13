"""Shared test fixtures and helpers for grocy_mcp e2e tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastmcp.client import Client


def unwrap_result(result: Any) -> Any:
    """Extract unwrapped structured content from a FastMCP CallToolResult.

    FastMCP wraps non-object return types (lists, unions) in {"result": ...}
    with the x-fastmcp-wrap-result flag. We use structured_content for test
    assertions (raw JSON-like access) and unwrap the synthetic wrapper.
    """
    sc = result.structured_content
    assert sc is not None, "No structured_content in CallToolResult"
    if isinstance(sc, dict) and len(sc) == 1 and "result" in sc:
        return sc["result"]
    return sc


@dataclass
class RefData:
    """Reference data created by ``create_ref_data`` for use in e2e tests."""

    suffix: str
    location: str
    qu: str
    group: str
    products: list[str] = field(default_factory=list)
    product_ids: list[int] = field(default_factory=list)


async def create_refunwrap_result(client: Client) -> RefData:
    """Create a location, QU, product group, and two products with uuid suffixes.

    Returns a ``RefData`` with the created names and IDs. Each call creates
    fresh entities (uuid-suffixed) so tests sharing a session-scoped Grocy
    container don't collide.
    """
    suffix = uuid.uuid4().hex[:6]
    loc_name = f"TestPantry-{suffix}"
    qu_name = f"TestBag-{suffix}"
    group_name = f"TestGroup-{suffix}"

    loc_results = unwrap_result(
        await client.call_tool(
            "locations_create",
            {"items": [{"name": loc_name, "description": "e2e fixture location", "is_freezer": False}]},
        )
    )
    assert loc_results[0]["kind"] == "ok", f"locations_create failed: {loc_results[0]}"

    qu_results = unwrap_result(
        await client.call_tool(
            "quantity_units_create",
            {"items": [{"name": qu_name, "name_plural": f"{qu_name}s", "description": "e2e fixture QU"}]},
        )
    )
    assert qu_results[0]["kind"] == "ok", f"quantity_units_create failed: {qu_results[0]}"

    group_results = unwrap_result(
        await client.call_tool(
            "product_groups_create", {"items": [{"name": group_name, "description": "e2e fixture group"}]}
        )
    )
    assert group_results[0]["kind"] == "ok", f"product_groups_create failed: {group_results[0]}"

    product_names = [f"TestRice-{suffix}", f"TestFlour-{suffix}"]
    create_results = unwrap_result(
        await client.call_tool(
            "products_create",
            {
                "items": [
                    {
                        "name": product_names[0],
                        "stock_qu": qu_name,
                        "location": loc_name,
                        "min_stock_amount": 1,
                        "description": "Test product for e2e",
                    },
                    {"name": product_names[1], "stock_qu": qu_name, "location": loc_name},
                ]
            },
        )
    )
    assert all(r["kind"] == "ok" for r in create_results), f"products_create failed: {create_results}"

    return RefData(
        suffix=suffix,
        location=loc_name,
        qu=qu_name,
        group=group_name,
        products=product_names,
        product_ids=[r["created_object_id"] for r in create_results],
    )
