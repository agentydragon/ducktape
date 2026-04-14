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


_E = ToolOverride
_D = lambda name, **kw: ToolOverride(name, enabled=False, **kw)  # noqa: E731

TOOL_OVERRIDES: dict[tuple[str, str], ToolOverride] = {
    # ── Generic entity CRUD ──────────────────────────────────────────
    # GET /objects/{entity}, POST /objects/{entity}, GET /objects/{entity}/{objectId}
    # and GET /stock are stripped from the OpenAPI spec by fix_openapi_spec.py;
    # they are replaced by batch tools in batch_tools.py.
    ("PUT", "/objects/{entity}/{objectId}"): _E("update_entity"),
    ("DELETE", "/objects/{entity}/{objectId}"): _E("delete_entity"),
    # ── Stock overview ───────────────────────────────────────────────
    ("GET", "/stock/volatile"): _E(
        "list_volatile_stock", "Returns products that are due soon, overdue, expired, or below min stock."
    ),
    # ── Stock entry ──────────────────────────────────────────────────
    ("GET", "/stock/entry/{entryId}"): _E("get_stock_entry"),
    ("PUT", "/stock/entry/{entryId}"): _E("edit_stock_entry"),
    ("GET", "/stock/entry/{entryId}/printlabel"): _D("print_stock_entry_label"),
    # ── Product stock operations (by ID) ─────────────────────────────
    ("GET", "/stock/products/{productId}"): _E("get_product_stock"),
    # POST /stock/products/{productId}/add, /consume, /inventory stripped from
    # OpenAPI spec; replaced by batch add_stock, consume_stock, inventory_products.
    ("POST", "/stock/products/{productId}/transfer"): _E("transfer_product_stock"),
    ("POST", "/stock/products/{productId}/open"): _E("open_product_stock"),
    ("GET", "/stock/products/{productId}/entries"): _E("list_product_stock_entries"),
    ("GET", "/stock/products/{productId}/locations"): _E("list_product_locations"),
    ("GET", "/stock/products/{productId}/price-history"): _D("get_product_price_history"),
    ("GET", "/stock/products/{productId}/printlabel"): _D("print_product_label"),
    # ── Product stock operations (by barcode) ────────────────────────
    ("GET", "/stock/products/by-barcode/{barcode}"): _D("get_product_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/add"): _D("add_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/consume"): _D("consume_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/transfer"): _D("transfer_stock_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/inventory"): _D("inventory_by_barcode"),
    ("POST", "/stock/products/by-barcode/{barcode}/open"): _D("open_stock_by_barcode"),
    # ── Product merge ────────────────────────────────────────────────
    ("POST", "/stock/products/{productIdToKeep}/merge/{productIdToRemove}"): _E("merge_products"),
    # ── Location stock ───────────────────────────────────────────────
    ("GET", "/stock/locations/{locationId}/entries"): _E("list_location_stock"),
    # ── Shopping list ────────────────────────────────────────────────
    ("POST", "/stock/shoppinglist/add-missing-products"): _E("shopping_list_add_missing"),
    ("POST", "/stock/shoppinglist/add-overdue-products"): _E("shopping_list_add_overdue"),
    ("POST", "/stock/shoppinglist/add-expired-products"): _E("shopping_list_add_expired"),
    ("POST", "/stock/shoppinglist/clear"): _E("shopping_list_clear"),
    ("POST", "/stock/shoppinglist/add-product"): _E("shopping_list_add_product"),
    ("POST", "/stock/shoppinglist/remove-product"): _E("shopping_list_remove_product"),
    # ── Bookings / transactions ──────────────────────────────────────
    ("GET", "/stock/bookings/{bookingId}"): _D("get_booking"),
    ("POST", "/stock/bookings/{bookingId}/undo"): _D("undo_booking"),
    ("GET", "/stock/transactions/{transactionId}"): _D("get_transaction_bookings"),
    ("POST", "/stock/transactions/{transactionId}/undo"): _D("undo_transaction"),
    # ── Barcode lookup ───────────────────────────────────────────────
    ("GET", "/stock/barcodes/external-lookup/{barcode}"): _D("barcode_lookup"),
    # ── Batteries ────────────────────────────────────────────────────
    ("GET", "/batteries"): _D("list_batteries"),
    ("GET", "/batteries/{batteryId}"): _D("get_battery"),
    ("POST", "/batteries/{batteryId}/charge"): _D("charge_battery"),
    ("POST", "/batteries/charge-cycles/{chargeCycleId}/undo"): _D("undo_battery_charge"),
    ("GET", "/batteries/{batteryId}/printlabel"): _D("print_battery_label"),
    # ── Chores ───────────────────────────────────────────────────────
    ("GET", "/chores"): _D("list_chores"),
    ("GET", "/chores/{choreId}"): _D("get_chore"),
    ("POST", "/chores/{choreId}/execute"): _D("execute_chore"),
    ("POST", "/chores/executions/{executionId}/undo"): _D("undo_chore_execution"),
    ("POST", "/chores/executions/calculate-next-assignments"): _D("recalculate_chore_assignments"),
    ("POST", "/chores/{choreIdToKeep}/merge/{choreIdToRemove}"): _D("merge_chores"),
    ("GET", "/chores/{choreId}/printlabel"): _D("print_chore_label"),
    # ── Tasks ────────────────────────────────────────────────────────
    ("GET", "/tasks"): _D("list_tasks"),
    ("POST", "/tasks/{taskId}/complete"): _D("complete_task"),
    ("POST", "/tasks/{taskId}/undo"): _D("undo_task"),
    # ── Recipes ──────────────────────────────────────────────────────
    ("GET", "/recipes/fulfillment"): _D("list_recipe_fulfillment"),
    ("GET", "/recipes/{recipeId}/fulfillment"): _D("get_recipe_fulfillment"),
    ("POST", "/recipes/{recipeId}/add-not-fulfilled-products-to-shoppinglist"): _D(
        "recipe_add_missing_to_shopping_list"
    ),
    ("POST", "/recipes/{recipeId}/consume"): _D("consume_recipe"),
    ("POST", "/recipes/{recipeId}/copy"): _D("copy_recipe"),
    ("GET", "/recipes/{recipeId}/printlabel"): _D("print_recipe_label"),
    # ── Calendar ─────────────────────────────────────────────────────
    ("GET", "/calendar/ical"): _D("get_calendar_ical"),
    ("GET", "/calendar/ical/sharing-link"): _D("get_calendar_sharing_link"),
    # ── Files ────────────────────────────────────────────────────────
    ("GET", "/files/{group}/{fileName}"): _E("get_file"),
    ("PUT", "/files/{group}/{fileName}"): _E("upload_file"),
    ("DELETE", "/files/{group}/{fileName}"): _D("delete_file"),
    # ── Users ────────────────────────────────────────────────────────
    ("GET", "/users"): _E("list_users"),
    ("POST", "/users"): _D("create_user"),
    ("PUT", "/users/{userId}"): _D("update_user"),
    ("DELETE", "/users/{userId}"): _D("delete_user"),
    ("GET", "/users/{userId}/permissions"): _D("get_user_permissions"),
    ("POST", "/users/{userId}/permissions"): _D("add_user_permission"),
    ("PUT", "/users/{userId}/permissions"): _D("set_user_permissions"),
    # ── Current user ─────────────────────────────────────────────────
    ("GET", "/user"): _E("get_current_user"),
    ("GET", "/user/settings"): _D("get_user_settings"),
    ("GET", "/user/settings/{settingKey}"): _D("get_user_setting"),
    ("PUT", "/user/settings/{settingKey}"): _D("set_user_setting"),
    ("DELETE", "/user/settings/{settingKey}"): _D("delete_user_setting"),
    # ── Userfields ───────────────────────────────────────────────────
    ("GET", "/userfields/{entity}/{objectId}"): _D("get_userfields"),
    ("PUT", "/userfields/{entity}/{objectId}"): _D("set_userfields"),
    # ── System ───────────────────────────────────────────────────────
    ("GET", "/system/info"): _E(
        "get_system_info"
    ),  # tool, not resource: claude.ai doesn't expose MCP resources to the AI
    ("GET", "/system/time"): _D("get_system_time"),
    ("GET", "/system/db-changed-time"): _E("get_db_changed_time"),
    ("GET", "/system/config"): _D("get_system_config"),
    ("GET", "/system/localization-strings"): _D("get_localization_strings"),
    ("POST", "/system/log-missing-localization"): _D("log_missing_localization"),
    # ── Print ────────────────────────────────────────────────────────
    ("GET", "/print/shoppinglist/thermal"): _D("print_shopping_list_thermal"),
}
