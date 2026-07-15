"""Tests for haku-console's grocy-sf reference router (`haku/console/tools/grocy.py`).

The reference endpoint reaches grocy-sf's own read tools through
`operator_authenticated_client`; these tests stub that seam with a fake client returning canned
structured content, so they cover the endpoint's parsing/flattening logic (including the
per-list `shopping_list_get` → `shopping_list_items` flatten that lets `shopping_list_item_edit`
/ `shopping_list_items_remove` previews resolve bare item IDs) without a live server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
import pytest_bazel

from haku.console.tools.grocy import GrocyShoppingListItem, grocy_sf_reference

# Structured content for grocy-sf's `*_list` read tools. List returns are FastMCP-wrapped under
# "result"; `shopping_list_get` (a dict return) is inlined, handled separately in `call_tool`.
_REFERENCE_ROWS: dict[str, dict[str, Any]] = {
    "products_list": {
        "result": [
            {
                "id": 1,
                "name": "Milk",
                "location_id": 10,
                "qu_id_stock": 20,
                "qu_id_purchase": 20,
                "qu_id_consume": 20,
                "min_stock_amount": 0,
                "default_best_before_days": 7,
                "due_type": 1,
            }
        ]
    },
    "locations_list": {"result": [{"id": 10, "name": "Pantry"}]},
    "quantity_units_list": {"result": [{"id": 20, "name": "pack"}]},
    "product_groups_list": {"result": [{"id": 30, "name": "Dairy"}]},
    "shopping_lists_list": {"result": [{"id": 1, "name": "Weekly"}, {"id": 2, "name": "Costco run"}]},
}

# Per-list `shopping_list_get` payloads (dict returns, items at the top level). Costco run is
# empty so the flatten covers the no-items list too.
_LIST_ITEMS: dict[int, dict[str, Any]] = {
    1: {
        "name": "Weekly",
        "description": None,
        "items": [
            {
                "item_id": 100,
                "product_name": "Milk",
                "product_id": 1,
                "amount": 2,
                "qu_name": "pack",
                "note": None,
                "done": False,
            },
            {
                "item_id": 101,
                "product_name": None,
                "product_id": None,
                "amount": 1,
                "qu_name": None,
                "note": "paper towels?",
                "done": False,
            },
        ],
    },
    2: {"name": "Costco run", "description": None, "items": []},
}


class _FakeResult:
    def __init__(self, structured_content: dict[str, Any]) -> None:
        self.structured_content = structured_content


class _FakeGrocyClient:
    """Minimal stand-in for the fastmcp `Client`: async `call_tool` over canned structured
    content, plus the async-context-manager protocol the endpoint (`async with ... as client`)
    uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> _FakeResult:
        self.calls.append((name, args))
        if name == "shopping_list_get":
            return _FakeResult(_LIST_ITEMS[args["shopping_list"]])
        return _FakeResult(_REFERENCE_ROWS[name])

    async def __aenter__(self) -> _FakeGrocyClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def test_reference_flattens_shopping_list_items(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGrocyClient()

    async def _fake_client(_server_id: str, _actor: Any, _settings: Any, _oauth_store: Any) -> _FakeGrocyClient:
        return fake

    monkeypatch.setattr("haku.console.tools.grocy.operator_authenticated_client", _fake_client)

    # The endpoint only touches actor/settings/oauth_store through `operator_authenticated_client`
    # (patched above), so plain Mock stand-ins suffice; typed `Any` to satisfy both mypy and pyright.
    actor: Any = Mock()
    settings: Any = Mock()
    oauth_store: Any = Mock()
    response = await grocy_sf_reference(actor=actor, settings=settings, oauth_store=oauth_store)

    # `shopping_list_get` is called once per list, in shopping_lists_list order.
    get_calls = [args for name, args in fake.calls if name == "shopping_list_get"]
    assert [args["shopping_list"] for args in get_calls] == [1, 2]

    # Every Weekly item is flattened in (Costco run empty), validated to the item model — the
    # extra `product_id` key the server includes is dropped.
    assert response.shopping_list_items == [
        GrocyShoppingListItem(item_id=100, product_name="Milk", note=None, amount=2, qu_name="pack", done=False),
        GrocyShoppingListItem(item_id=101, product_name=None, note="paper towels?", amount=1, qu_name=None, done=False),
    ]
    # The other reference maps still populate.
    assert [sl.name for sl in response.shopping_lists] == ["Weekly", "Costco run"]
    assert [p.name for p in response.products] == ["Milk"]


if __name__ == "__main__":
    pytest_bazel.main()
