"""Types modelling Grocy's API surface.

Entity type enums, writable field sets, and Pydantic models for Grocy REST
responses we destructure. These correspond to Grocy's schema — not to the
MCP tool I/O types (see mcp_types.py for those).
"""

from __future__ import annotations

from enum import StrEnum

# ── Entity types exposed by the resolver ────────────────────────────────────


class EntityType(StrEnum):
    """Entity types the resolver can look up by name or ID.

    Values are the Grocy API path segments (``/objects/{value}``).
    """

    PRODUCT = "products"
    LOCATION = "locations"
    QUANTITY_UNIT = "quantity_units"
    PRODUCT_GROUP = "product_groups"
    SHOPPING_LIST = "shopping_lists"


# ── Entity types for the batch CRUD tools ───────────────────────────────────


class WriteableEntityType(StrEnum):
    """Grocy entity types that accept create / edit / delete via ``/objects/{entity}``.

    Used by ``entities_create``. Strict subset of ``ReadableEntityType``:
    excludes the view-only ``_view`` / ``_resolved`` variants and the computed
    aggregates Grocy itself marks ``ExposedEntityNoEdit``, and excludes
    ``shopping_list`` — the typed shopping-list tools cover items end-to-end
    (``shopping_list_items_add``, ``shopping_list_item_edit``,
    ``shopping_list_items_remove``, ``shopping_list_clear``).
    """

    PRODUCTS = "products"
    PRODUCT_BARCODES = "product_barcodes"
    PRODUCT_GROUPS = "product_groups"
    LOCATIONS = "locations"
    SHOPPING_LOCATIONS = "shopping_locations"
    SHOPPING_LISTS = "shopping_lists"  # list metadata; items via the typed shopping_list tools
    QUANTITY_UNITS = "quantity_units"
    QUANTITY_UNIT_CONVERSIONS = "quantity_unit_conversions"
    RECIPES = "recipes"
    RECIPES_POS = "recipes_pos"
    RECIPES_NESTINGS = "recipes_nestings"
    MEAL_PLAN = "meal_plan"
    MEAL_PLAN_SECTIONS = "meal_plan_sections"
    TASKS = "tasks"
    TASK_CATEGORIES = "task_categories"
    CHORES = "chores"
    BATTERIES = "batteries"
    EQUIPMENT = "equipment"
    USERFIELDS = "userfields"
    USERENTITIES = "userentities"
    USEROBJECTS = "userobjects"
    API_KEYS = "api_keys"


class ReadableEntityType(StrEnum):
    """Grocy entity types exposed for read via ``entities_list`` / ``entities_get``.

    Superset of ``WriteableEntityType`` plus the entities Grocy publishes as
    ``ExposedEntityNoEdit``: SQL views (``_view`` / ``_resolved``), append-only
    audit tables (``stock_log``, ``chores_log``, ``battery_charge_cycles``), and
    computed aggregates (``stock``, ``stock_current_locations``,
    ``products_last_purchased``, ``products_average_price``,
    ``permission_hierarchy``).
    """

    # ── Writeable ────────────────────────────────────────────────────────
    PRODUCTS = "products"
    PRODUCT_BARCODES = "product_barcodes"
    PRODUCT_GROUPS = "product_groups"
    LOCATIONS = "locations"
    SHOPPING_LOCATIONS = "shopping_locations"
    SHOPPING_LISTS = "shopping_lists"
    QUANTITY_UNITS = "quantity_units"
    QUANTITY_UNIT_CONVERSIONS = "quantity_unit_conversions"
    RECIPES = "recipes"
    RECIPES_POS = "recipes_pos"
    RECIPES_NESTINGS = "recipes_nestings"
    MEAL_PLAN = "meal_plan"
    MEAL_PLAN_SECTIONS = "meal_plan_sections"
    TASKS = "tasks"
    TASK_CATEGORIES = "task_categories"
    CHORES = "chores"
    BATTERIES = "batteries"
    EQUIPMENT = "equipment"
    USERFIELDS = "userfields"
    USERENTITIES = "userentities"
    USEROBJECTS = "userobjects"
    API_KEYS = "api_keys"
    # ── Read-only (Grocy ExposedEntityNoEdit) ────────────────────────────
    STOCK = "stock"
    STOCK_LOG = "stock_log"
    STOCK_CURRENT_LOCATIONS = "stock_current_locations"
    CHORES_LOG = "chores_log"
    PRODUCTS_LAST_PURCHASED = "products_last_purchased"
    PRODUCTS_AVERAGE_PRICE = "products_average_price"
    QUANTITY_UNIT_CONVERSIONS_RESOLVED = "quantity_unit_conversions_resolved"
    RECIPES_POS_RESOLVED = "recipes_pos_resolved"
    BATTERY_CHARGE_CYCLES = "battery_charge_cycles"
    PRODUCT_BARCODES_VIEW = "product_barcodes_view"
    PERMISSION_HIERARCHY = "permission_hierarchy"


# Sanity check: every WriteableEntityType must be a valid ReadableEntityType.
assert {t.value for t in WriteableEntityType} <= {t.value for t in ReadableEntityType}


# ── Product writable fields ─────────────────────────────────────────────────

# Writable columns on the products table (migration 0207 + 0210 + 0219).
# Grocy's GET returns computed view fields (has_sub_products, qu_factor_*, etc.)
# that are rejected on PUT. Only send these columns.
PRODUCT_WRITABLE_FIELDS: set[str] = {
    "name",
    "description",
    "product_group_id",
    "active",
    "location_id",
    "shopping_location_id",
    "qu_id_purchase",
    "qu_id_stock",
    "qu_id_consume",
    "qu_id_price",
    "min_stock_amount",
    "default_best_before_days",
    "default_best_before_days_after_open",
    "default_best_before_days_after_freezing",
    "default_best_before_days_after_thawing",
    "picture_file_name",
    "enable_tare_weight_handling",
    "tare_weight",
    "not_check_stock_fulfillment_for_recipes",
    "parent_product_id",
    "calories",
    "cumulate_min_stock_amount_of_sub_products",
    "due_type",
    "quick_consume_amount",
    "hide_on_stock_overview",
    "default_stock_label_type",
    "should_not_be_frozen",
    "treat_opened_as_out_of_stock",
    "no_own_stock",
}
