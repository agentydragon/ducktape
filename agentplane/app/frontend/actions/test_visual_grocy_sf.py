"""grocy_sf Action presentation examples."""

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

_KEFIR_ENTRY = {
    "entry_id": 12,
    "product_id": 21,
    "product_name": "Kefir",
    "amount": 2,
    "qu_name": "Bottle",
    "location_name": "Fridge",
    "best_before_date": "2026-11-25",
    "purchased_date": "2026-11-19",
    "price": 3.2,
    "open": True,
    "note": "Cultured",
}

_GROCY_ITEM_PREVIEW: dict[str, object] = {
    "references": [
        {
            "key": "shopping_list/52",
            "name": "Kefir",
            "shoppingItemCurrent": {"listName": "Weekly groceries", "amount": 2, "unitName": "Bottle", "done": False},
        }
    ]
}

_KEFIR_CHANGE = {
    "kind": "ok",
    "product_name": "Kefir",
    "transaction_id": "tx-kefir",
    "qu_name": "Bottle",
    "location_name": "Fridge",
}

_KEFIR_ALERT = {"product_id": 21, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "grocy_sf",
            "products_list",
            {"detail": "full"},
            [{"id": 1, "name": "Olive Oil", "min_stock_amount": 1}, {"id": 2, "name": "Oats"}],
            "Minimum 1 in stock",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "quantity_units_list",
            {"detail": "full"},
            [{"id": 1, "name": "Bottle", "name_plural": "Bottles"}, {"id": 2, "name": "Gram", "name_plural": "Grams"}],
            "Plural: Bottles",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "get_system_info",
            {},
            {"grocy_version": {"Version": "4.5.0"}, "php_version": "8.4", "sqlite_version": "3.50", "db_version": 1},
            "Grocy 4.5.0",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "products_edit",
            {"items": [{"product": 2, "name": "Rolled oats", "location": 5, "clear_fields": ["description"]}]},
            [{"kind": "ok", "object_id": 2}],
            "1 product edited",
            live_preview=(
                "grocy-reference-summaries",
                {
                    "references": [
                        {
                            "key": "product_edits/2",
                            "name": "Oats",
                            "productEditCurrent": {"locationId": "4", "description": "Keep dry"},
                        },
                        {"key": "locations/4", "name": "Cupboard"},
                        {"key": "locations/5", "name": "Pantry"},
                    ]
                },
                "Cupboard",
            ),
        ),
        ActionExample(
            "grocy_sf",
            "stock_get",
            {"products": ["Kefir"], "locations": ["Fridge"]},
            [
                {
                    "product_id": 21,
                    "product_name": "Kefir",
                    "amount": 3,
                    "amount_opened": 1,
                    "qu_name": "Bottle",
                    "location_name": "Fridge",
                    "best_before_date": "2026-11-25",
                }
            ],
            "1 opened · Best before 2026-11-25",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "stock_add",
            {"items": [{"product": "Kefir", "location": "Fridge", "qu": "Bottle", "amount": 2, "price": 3.2}]},
            [{**_KEFIR_CHANGE, "amount_delta": 2, "new_amount": 4, "entry_id": 12, "best_before_date": "2026-11-25"}],
            "Now 4 Bottle total",
        ),
        ActionExample(
            "grocy_sf",
            "stock_consume",
            {"items": [{"product": "Kefir", "location": "Fridge", "qu": "Bottle", "amount": 1, "spoiled": True}]},
            [{**_KEFIR_CHANGE, "amount_delta": -1, "new_amount": 1}],
            "Change · -1 Bottle",
        ),
        ActionExample(
            "grocy_sf",
            "stock_set",
            {"items": [{"product": "Kefir", "location": "Fridge", "new_amount": 0}]},
            [{**_KEFIR_CHANGE, "amount_delta": -2, "new_amount": 0}],
            "Now 0 Bottle total",
        ),
        ActionExample(
            "grocy_sf",
            "stock_transfer",
            {"product": "Kefir", "qu": "Bottle", "amount": 1, "from_location": "Fridge", "to_location": "Cellar"},
            {**_KEFIR_CHANGE, "location_name": "Fridge -> Cellar"},
            "Kefir · Fridge → Cellar",
        ),
        ActionExample(
            "grocy_sf",
            "stock_entries_list",
            {"products": ["Kefir"]},
            [{"kind": "ok", "entry": _KEFIR_ENTRY}],
            "Kefir · 2 Bottle in Fridge",
        ),
        ActionExample(
            "grocy_sf",
            "stock_entry_edit",
            {"items": [{"entry_id": 12, "amount": 2, "clear_fields": ["price"]}]},
            [
                {
                    "kind": "ok",
                    "entry": {**_KEFIR_ENTRY, "price": None},
                    "changes": {"amount": {"old": 3, "new": 2}, "price": {"old": 3.2, "new": None}},
                }
            ],
            "Amount · 3 → 2",
        ),
        ActionExample(
            "grocy_sf",
            "get_product_stock",
            {"productId": 21},
            {
                "product": {"id": "21", "name": "Kefir"},
                "stock_amount": "2",
                "stock_amount_opened": "1",
                "quantity_unit_stock": {"name": "Bottle", "name_plural": "Bottles"},
                "location": {"name": "Fridge"},
                "next_due_date": "2026-11-25",
            },
            "Kefir · 2 Bottles",
        ),
        ActionExample(
            "grocy_sf",
            "get_expiring_stock",
            {"days_ahead": 7},
            [{**_KEFIR_ALERT, "location_name": "Fridge", "best_before_date": "2026-11-25", "days_until_expiry": 6}],
            "Due in 6 d",
        ),
        ActionExample(
            "grocy_sf",
            "get_expired_stock",
            {},
            [{**_KEFIR_ALERT, "location_name": "Fridge", "best_before_date": "2026-11-17", "days_overdue": 2}],
            "2 d overdue",
        ),
        ActionExample(
            "grocy_sf",
            "get_below_minimum_stock",
            {},
            [{**_KEFIR_ALERT, "min_amount": 3, "deficit": 1}],
            "Minimum 3 · 1 short",
        ),
        ActionExample(
            "grocy_sf",
            "list_volatile_stock",
            {"due_soon_days": 6},
            {
                "due_products": [
                    {"product_id": "21", "amount": "2", "best_before_date": "2026-11-25", "product": {"name": "Kefir"}}
                ],
                "overdue_products": [],
                "expired_products": [],
                "missing_products": [{"id": "22", "name": "Oats", "amount_missing": "2"}],
            },
            "Oats · 2 stock units short",
        ),
        ActionExample(
            "grocy_sf",
            "open_product_stock",
            {"productId": 21, "amount": 1, "stock_entry_id": "batch-kefir"},
            [{"product_id": "21", "amount": "1", "transaction_id": "tx-open", "transaction_type": "product-opened"}],
            "Marked 1 stock units as opened",
        ),
        ActionExample("grocy_sf", "transaction_undo", {"transactionId": "tx-kefir"}, "null", "Transaction undone"),
        ActionExample(
            "grocy_sf",
            "shopping_list_get",
            {"shopping_list": "Weekly groceries"},
            {
                "name": "Weekly groceries",
                "items": [
                    {
                        "item_id": 52,
                        "product_id": 21,
                        "product_name": "Kefir",
                        "amount": 2,
                        "qu_name": "Bottle",
                        "note": "Plain",
                        "done": True,
                    }
                ],
            },
            "Kefir · Done",
        ),
        ActionExample(
            "grocy_sf",
            "shopping_list_clear",
            {"shopping_list": "Weekly groceries"},
            {"kind": "ok", "cleared": 4},
            "4 items removed · Shopping list kept",
        ),
        ActionExample(
            "grocy_sf",
            "shopping_list_items_add",
            {"items": [{"shopping_list": "Weekly groceries", "product": "Kefir", "amount": 2, "note": "Plain"}]},
            [{"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}],
            "1 item added",
        ),
        ActionExample(
            "grocy_sf",
            "shopping_list_items_remove",
            {"item_ids": [52]},
            [{"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"}],
            "1 item removed",
            live_preview=("grocy-reference-summaries", _GROCY_ITEM_PREVIEW, "Kefir"),
        ),
        ActionExample(
            "grocy_sf",
            "shopping_list_item_edit",
            {"item_id": 52, "done": True, "clear_fields": ["note"]},
            {"kind": "ok", "item_id": 52, "product_name": "Kefir", "amount": 2, "qu_name": "Bottle"},
            "1 item updated",
            live_preview=("grocy-reference-summaries", _GROCY_ITEM_PREVIEW, "Kefir"),
        ),
        ActionExample(
            "grocy_sf",
            "shopping_lists_list",
            {"detail": "full"},
            [{"id": 3, "name": "Weekly groceries", "description": "Regular restock"}],
            "Weekly groceries · Regular restock",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "shopping_lists_create",
            {"items": [{"name": "Farmers market", "description": "Saturday"}]},
            [{"kind": "ok", "created_object_id": 3}],
            "Created Farmers market",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "products_create",
            {
                "items": [
                    {
                        "name": "Kefir",
                        "location": "Fridge",
                        "stock_qu": "Bottle",
                        "due_type": 2,
                        "default_best_before_days": 7,
                    }
                ]
            },
            [{"kind": "ok", "created_object_id": 21}],
            "Created Kefir",
        ),
        ActionExample(
            "grocy_sf",
            "product_delete",
            {"product": "Old coffee"},
            {"kind": "ok", "object_id": 31},
            "Deleted Old coffee and its stock entries",
        ),
        ActionExample(
            "grocy_sf",
            "products_merge",
            {"productIdToKeep": 16, "productIdToRemove": 15},
            "null",
            "Products merged",
            live_preview=(
                "grocy-reference-summaries",
                {"references": [{"key": "products/16", "name": "Ground coffee"}]},
                "Ground coffee",
            ),
        ),
        ActionExample(
            "grocy_sf",
            "locations_create",
            {"items": [{"name": "Chest freezer", "is_freezer": True}]},
            [{"kind": "ok", "created_object_id": 4}],
            "Created Chest freezer",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "locations_list",
            {"detail": "full"},
            [{"id": 4, "name": "Chest freezer", "is_freezer": "1"}],
            "Chest freezer · Freezer",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "product_groups_create",
            {"items": [{"name": "Breakfast", "description": "Morning staples"}]},
            [{"kind": "ok", "created_object_id": 5}],
            "Created Breakfast",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "product_groups_list",
            {"detail": "full"},
            [{"id": 5, "name": "Breakfast", "description": "Morning staples"}],
            "Breakfast · Morning staples",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "quantity_units_create",
            {"items": [{"name": "Bottle", "name_plural": "Bottles"}]},
            [{"kind": "ok", "created_object_id": 6}],
            "Created Bottle",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "entities_create",
            {"items": [{"entity_type": "tasks", "body": {"name": "Clean fridge", "due_date": "2026-11-22"}}]},
            [{"kind": "ok", "created_object_id": 41}],
            "Created Clean fridge",
        ),
        ActionExample(
            "grocy_sf",
            "entities_get",
            {"entity_type": "product_groups", "object_ids": [5]},
            [
                {
                    "kind": "ok",
                    "entity_type": "product_groups",
                    "object_id": 5,
                    "data": {"id": "5", "name": "Breakfast", "description": "Morning staples"},
                }
            ],
            "Description · Morning staples",
        ),
        ActionExample(
            "grocy_sf",
            "entities_list",
            {"entity_types": ["quantity_units", "product_groups"]},
            {
                "quantity_units": [{"id": "6", "name": "Bottle", "name_plural": "Bottles"}],
                "product_groups": [{"id": "5", "name": "Breakfast"}],
            },
            "Plural · Bottles",
        ),
        ActionExample(
            "grocy_sf", "entity_delete", {"entity": "product_groups", "objectId": 5}, "null", "Record deleted"
        ),
        ActionExample(
            "grocy_sf",
            "entity_update",
            {"entity": "product_groups", "objectId": 5, "body": {"description": "Pantry basics"}},
            "null",
            "Record updated",
        ),
        ActionExample(
            "grocy_sf",
            "file_get",
            {
                "group": "productpictures",
                "fileName": "a2VmaXIucG5n",
                "force_serve_as": "picture",
                "best_fit_width": 320,
            },
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9iUAAAAASUVORK5CYII=",
            "File contents returned",
        ),
        ActionExample(
            "grocy_sf",
            "file_upload",
            {"group": "productpictures", "fileName": "a2VmaXIucG5n", "body": "aW1hZ2UgYnl0ZXM="},
            "null",
            "File uploaded",
        ),
        ActionExample(
            "grocy_sf",
            "get_current_user",
            {},
            {"id": 4, "display_name": "Casey Example", "username": "casey"},
            "Casey Example · casey",
            compact_card_expands=False,
        ),
        ActionExample(
            "grocy_sf",
            "get_db_changed_time",
            {},
            {"changed_time": "2026-11-19 18:20:13"},
            "Grocy database last changed · 2026-11-19 18:20:13",
            compact_card_expands=False,
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
