"""Tool naming and description overrides for Grocy MCP tools.

Grocy's OpenAPI spec has no ``operationId``s, so FastMCP generates tool
names by slugifying the verbose ``summary`` text and truncating to 56
chars. This module provides a ``(method, path) → ToolOverride`` mapping
that assigns concise names and optional extra context for the LLM.

The mapping is keyed on ``(HTTP method, path template)`` — both are
stable across Grocy versions. If a new version adds a route we don't
have an override for, the server crashes at startup so it can be added.

Tools can be disabled by setting ``enabled=False``. Disabled tools are
excluded from the MCP server entirely. Tools with ``resource=True`` are
exposed as MCP resources instead of tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolOverride:
    name: str
    extra_description: str | None = None
    enabled: bool = True
    resource: bool = False
    tags: set[str] = field(default_factory=set)


_enabled = ToolOverride
_disabled = lambda name, **kw: ToolOverride(name, enabled=False, **kw)  # noqa: E731

TOOL_OVERRIDES: dict[tuple[str, str], ToolOverride] = {
    # ── Generic entity CRUD ──────────────────────────────────────────
    # GET /objects/{entity}, POST /objects/{entity}, GET /objects/{entity}/{objectId}
    # and GET /stock are stripped from the OpenAPI spec by fix_openapi_spec.py;
    # they are replaced by batch tools in batch_tools.py.
    ("PUT", "/objects/{entity}/{objectId}"): _enabled(
        "entity_update",
        "WARNING: full replace, not a partial update — every field you omit gets nulled. Read the "
        "current row with `entities_get` first, then send the complete object with your changes "
        "applied. For products specifically, prefer the typed `product_edit` (does the read-merge-write "
        "for you and only touches writable columns).",
    ),
    ("DELETE", "/objects/{entity}/{objectId}"): _enabled("entity_delete"),
    # ── Stock overview ───────────────────────────────────────────────
    ("GET", "/stock/volatile"): _enabled(
        "list_volatile_stock",
        "Returns products that are due soon, overdue, expired, or below min stock — the same data "
        "the typed `get_expiring_stock` / `get_expired_stock` / `get_below_minimum_stock` tools slice.",
    ),
    # ── Stock entry ──────────────────────────────────────────────────
    # Replaced by custom tools in batch_tools.py (stock_entries_list, stock_entry_edit).
    # Stripped from the OpenAPI spec by fix_openapi_spec.py.
    ("GET", "/stock/entry/{entryId}"): _disabled("get_stock_entry"),
    ("PUT", "/stock/entry/{entryId}"): _disabled("stock_entry_edit"),
    ("GET", "/stock/entry/{entryId}/printlabel"): _disabled("print_stock_entry_label"),
    # ── Product stock operations (by ID) ─────────────────────────────
    ("GET", "/stock/products/{productId}"): _enabled("get_product_stock"),
    # POST /stock/products/{productId}/add, /consume, /inventory stripped from
    # OpenAPI spec; replaced by batch stock_add, stock_consume, stock_set.
    # Replaced by custom stock_transfer in batch_tools.py.
    # Stripped from the OpenAPI spec by fix_openapi_spec.py.
    ("POST", "/stock/products/{productId}/transfer"): _disabled("transfer_product_stock"),
    ("POST", "/stock/products/{productId}/open"): _enabled("open_product_stock"),
    # `list_product_stock_entries` / `list_product_locations` disabled: the
    # generated schema carries a `query[]` filter parameter, whose property
    # key violates Anthropic's tool schema regex
    # `^[a-zA-Z0-9_.-]{1,64}$`. TODO: re-expose as hand-written batch tools
    # once there's demand.
    ("GET", "/stock/products/{productId}/entries"): _disabled("list_product_stock_entries"),
    ("GET", "/stock/products/{productId}/locations"): _disabled("list_product_locations"),
    ("GET", "/stock/products/{productId}/price-history"): _disabled("get_product_price_history"),
    ("GET", "/stock/products/{productId}/printlabel"): _disabled("print_product_label"),
    # ── Product stock operations (by barcode) ────────────────────────
    ("GET", "/stock/products/by-barcode/{barcode}"): _disabled("get_product_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/add"): _disabled("add_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/consume"): _disabled("consume_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/transfer"): _disabled("transfer_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/inventory"): _disabled("inventory_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/open"): _disabled("open_stock_by_barcode"),
    # ── Product merge ────────────────────────────────────────────────
    ("POST", "/stock/products/{productIdToKeep}/merge/{productIdToRemove}"): _enabled("products_merge"),
    # ── Location stock ───────────────────────────────────────────────
    # `list_location_stock` disabled: same `query[]` issue as above.
    ("GET", "/stock/locations/{locationId}/entries"): _disabled("list_location_stock"),
    # ── Shopping list ────────────────────────────────────────────────
    # Shopping list bulk helpers — disabled for now (low usage, custom tools cover the core).
    ("POST", "/stock/shoppinglist/add-missing-products"): _disabled("shopping_list_add_missing"),
    ("POST", "/stock/shoppinglist/add-overdue-products"): _disabled("shopping_list_add_overdue"),
    ("POST", "/stock/shoppinglist/add-expired-products"): _disabled("shopping_list_add_expired"),
    # Replaced by custom tools in batch_tools.py.
    # Stripped from the OpenAPI spec by fix_openapi_spec.py.
    ("POST", "/stock/shoppinglist/clear"): _disabled("shopping_list_clear"),
    ("POST", "/stock/shoppinglist/add-product"): _disabled("shopping_list_add_product"),
    ("POST", "/stock/shoppinglist/remove-product"): _disabled("shopping_list_remove_product"),
    # ── Bookings / transactions ──────────────────────────────────────
    ("GET", "/stock/bookings/{bookingId}"): _disabled("get_booking"),
    ("POST", "/stock/bookings/{bookingId}/undo"): _disabled("undo_booking"),
    ("GET", "/stock/transactions/{transactionId}"): _disabled("get_transaction_bookings"),
    ("POST", "/stock/transactions/{transactionId}/undo"): _enabled("transaction_undo"),
    # ── Barcode lookup ───────────────────────────────────────────────
    ("GET", "/stock/barcodes/external-lookup/{barcode}"): _disabled("barcode_lookup"),
    # ── Batteries ────────────────────────────────────────────────────
    ("GET", "/batteries"): _disabled("list_batteries"),
    ("GET", "/batteries/{batteryId}"): _disabled("get_battery"),
    ("POST", "/batteries/{batteryId}/charge"): _disabled("charge_battery"),
    ("POST", "/batteries/charge-cycles/{chargeCycleId}/undo"): _disabled("undo_battery_charge"),
    ("GET", "/batteries/{batteryId}/printlabel"): _disabled("print_battery_label"),
    # ── Chores ───────────────────────────────────────────────────────
    ("GET", "/chores"): _disabled("list_chores"),
    ("GET", "/chores/{choreId}"): _disabled("get_chore"),
    ("POST", "/chores/{choreId}/execute"): _disabled("execute_chore"),
    ("POST", "/chores/executions/{executionId}/undo"): _disabled("undo_chore_execution"),
    ("POST", "/chores/executions/calculate-next-assignments"): _disabled("recalculate_chore_assignments"),
    ("POST", "/chores/{choreIdToKeep}/merge/{choreIdToRemove}"): _disabled("merge_chores"),
    ("GET", "/chores/{choreId}/printlabel"): _disabled("print_chore_label"),
    # ── Tasks ────────────────────────────────────────────────────────
    ("GET", "/tasks"): _disabled("list_tasks"),
    ("POST", "/tasks/{taskId}/complete"): _disabled("complete_task"),
    ("POST", "/tasks/{taskId}/undo"): _disabled("undo_task"),
    # ── Recipes ──────────────────────────────────────────────────────
    ("GET", "/recipes/fulfillment"): _disabled("list_recipe_fulfillment"),
    ("GET", "/recipes/{recipeId}/fulfillment"): _disabled("get_recipe_fulfillment"),
    ("POST", "/recipes/{recipeId}/add-not-fulfilled-products-to-shoppinglist"): _disabled(
        "recipe_add_missing_to_shopping_list"
    ),
    ("POST", "/recipes/{recipeId}/consume"): _disabled("consume_recipe"),
    ("POST", "/recipes/{recipeId}/copy"): _disabled("copy_recipe"),
    ("GET", "/recipes/{recipeId}/printlabel"): _disabled("print_recipe_label"),
    # ── Calendar ─────────────────────────────────────────────────────
    ("GET", "/calendar/ical"): _disabled("get_calendar_ical"),
    ("GET", "/calendar/ical/sharing-link"): _disabled("get_calendar_sharing_link"),
    # ── Files ────────────────────────────────────────────────────────
    ("GET", "/files/{group}/{fileName}"): _enabled("file_get"),
    ("PUT", "/files/{group}/{fileName}"): _enabled("file_upload"),
    ("DELETE", "/files/{group}/{fileName}"): _disabled("delete_file"),
    # ── Users ────────────────────────────────────────────────────────
    # `list_users` disabled: same `query[]` issue as above.
    ("GET", "/users"): _disabled("list_users"),
    ("POST", "/users"): _disabled("create_user"),
    ("PUT", "/users/{userId}"): _disabled("update_user"),
    ("DELETE", "/users/{userId}"): _disabled("delete_user"),
    ("GET", "/users/{userId}/permissions"): _disabled("get_user_permissions"),
    ("POST", "/users/{userId}/permissions"): _disabled("add_user_permission"),
    ("PUT", "/users/{userId}/permissions"): _disabled("set_user_permissions"),
    # ── Current user ─────────────────────────────────────────────────
    ("GET", "/user"): _enabled("get_current_user"),
    ("GET", "/user/settings"): _disabled("get_user_settings"),
    ("GET", "/user/settings/{settingKey}"): _disabled("get_user_setting"),
    ("PUT", "/user/settings/{settingKey}"): _disabled("set_user_setting"),
    ("DELETE", "/user/settings/{settingKey}"): _disabled("delete_user_setting"),
    # ── Userfields ───────────────────────────────────────────────────
    ("GET", "/userfields/{entity}/{objectId}"): _disabled("get_userfields"),
    ("PUT", "/userfields/{entity}/{objectId}"): _disabled("set_userfields"),
    # ── System ───────────────────────────────────────────────────────
    ("GET", "/system/info"): _enabled(
        "get_system_info"
    ),  # tool, not resource: claude.ai doesn't expose MCP resources to the AI
    ("GET", "/system/time"): _disabled("get_system_time"),
    ("GET", "/system/db-changed-time"): _enabled("get_db_changed_time"),
    ("GET", "/system/config"): _disabled("get_system_config"),
    ("GET", "/system/localization-strings"): _disabled("get_localization_strings"),
    ("POST", "/system/log-missing-localization"): _disabled("log_missing_localization"),
    # ── Print ────────────────────────────────────────────────────────
    ("GET", "/print/shoppinglist/thermal"): _disabled("print_shopping_list_thermal"),
}
