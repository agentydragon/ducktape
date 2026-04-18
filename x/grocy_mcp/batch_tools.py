"""Batch operation tools for Grocy MCP server.

Custom tools that replace several single-shot OpenAPI-generated tools with
batch/enriched versions. Each operation fans out concurrently via asyncio.gather
and collects results (continue-and-collect, never fail-fast).

Concurrency is bounded by an asyncio.Semaphore so that large batches don't
overwhelm Grocy's PHP/SQLite backend or the Authentik token exchange endpoint
(each request triggers a per-user JWT exchange). Transient errors (timeouts,
5xx) are retried with exponential backoff.

IMPORTANT: Only idempotent/read operations go inside _retry(). Mutating POSTs
are retried only for the mutation itself — the follow-up GET (to read new_amount)
is best-effort outside the retry loop. This prevents retry-induced stock inflation
(see 2026-04-17 incident: products 90, 95, 97).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, model_validator

from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.resolver import EntityResolver

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    """Parse a Grocy ISO date string to a date object. Returns None for null/empty."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        logger.warning("unparseable date from Grocy: %r", value)
        return None


def _date_to_str(value: date | None) -> str | None:
    """Convert a date to ISO string for Grocy API. None stays None."""
    return value.isoformat() if value is not None else None


def _format_exc(e: Exception) -> str:
    """Format exception with full traceback for error reporting.

    On `HTTPStatusError`, append Grocy's response body so the agent sees
    the actual failure reason (Grocy returns a JSON `error_message` on
    most 4xx/5xx); httpx's default error is just the status + URL.
    """
    tb = "".join(traceback.format_exception(e))
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text.strip()
        if body:
            # Truncate so a ~1MB HTML error page doesn't swamp the agent
            # context; the first ~1.5KB carries Grocy's JSON error message.
            return f"{tb}\nGrocy response body: {body[:1500]}"
    return tb


def _is_retryable(exc: Exception) -> bool:
    """Whether an exception is transient and worth retrying."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429, 500, 502, 503, 504}


async def _with_retry[T](
    fn: Callable[[], Awaitable[T]], semaphore: asyncio.Semaphore, *, max_retries: int, base_delay: float
) -> T:
    """Run fn under semaphore with retry on transient errors."""
    async with semaphore:
        for attempt in range(1 + max_retries):
            try:
                return await fn()
            except Exception as e:
                if not _is_retryable(e) or attempt == max_retries:
                    raise
                delay = base_delay * (2**attempt)
                logger.warning(
                    "retryable error (attempt %d/%d, next in %.1fs): %s", attempt + 1, 1 + max_retries, delay, e
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")


# ── Entity types ──────────────────────────────────────────────────────────────


class WriteableEntityType(StrEnum):
    """Grocy entity types that accept create / edit / delete via `/objects/{entity}`.

    Used by `entities_create`. Strict subset of `ReadableEntityType`:
    excludes the view-only `_view` / `_resolved` variants and the computed
    aggregates Grocy itself marks `ExposedEntityNoEdit`, and excludes
    `shopping_list` — the typed shopping-list tools cover items end-to-end
    (`shopping_list_items_add`, `shopping_list_item_edit`,
    `shopping_list_items_remove`, `shopping_list_clear`).
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
    """Grocy entity types exposed for read via `entities_list` / `entities_get`.

    Superset of `WriteableEntityType` plus the entities Grocy publishes as
    `ExposedEntityNoEdit`: SQL views (`_view` / `_resolved`), append-only
    audit tables (`stock_log`, `chores_log`, `battery_charge_cycles`), and
    computed aggregates (`stock`, `stock_current_locations`,
    `products_last_purchased`, `products_average_price`,
    `permission_hierarchy`).
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


# ── Shared input/output types ────────────────────────────────────────────────


class CreateItem(BaseModel):
    entity_type: WriteableEntityType
    body: dict[str, Any]


class CreateOk(BaseModel):
    kind: Literal["ok"] = "ok"
    created_object_id: int | None = None


class CreateError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class GetOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entity_type: ReadableEntityType
    object_id: int
    data: dict[str, Any]


class GetError(BaseModel):
    kind: Literal["error"] = "error"
    entity_type: ReadableEntityType
    object_id: int
    error: str


# ── Stock overview response ──────────────────────────────────────────────────


class StockEntry(BaseModel):
    """Compact stock overview entry. Names always included, no nested dicts."""

    product_id: int
    product_name: str
    amount: float
    amount_opened: float
    qu_name: str
    location_name: str
    best_before_date: date | None = None


# ── Stock mutation input/output ──────────────────────────────────────────────


# Reused field descriptions for stock-mutation item fields. Stating the QU
# constraint in one place keeps `stock_add` / `stock_consume` /
# `stock_set` / `stock_transfer` honest about it.
_PRODUCT_DESC = "Product. Name or ID."
_QU_DESC = (
    "Quantity unit. Name or ID. Must match the product's stock QU or have a defined conversion "
    "(see the `quantity_unit_conversions` entity)."
)
_BEST_BEFORE_DESC = "Best-before date in `YYYY-MM-DD` format. Omit for no expiry."
_PRICE_DESC = "Per-unit price. Omit to use the last recorded price."
_DETAIL_DESC = "`brief` returns only `id` + `name`. `full` returns every column."


class AddItem(BaseModel):
    """One stock addition for `stock_add`."""

    product: int | str = Field(description=_PRODUCT_DESC)
    amount: float = Field(description="Amount to add, in `qu` units.")
    qu: int | str = Field(description=_QU_DESC)
    location: int | str = Field(description="Storage location to add to. Name or ID.")
    best_before_date: date | None = Field(default=None, description=_BEST_BEFORE_DESC)
    price: float | None = Field(default=None, description=_PRICE_DESC)
    note: str | None = Field(default=None, description="Free-text note attached to the resulting stock entry.")


class ConsumeItem(BaseModel):
    """One stock consumption for `stock_consume`."""

    product: int | str = Field(description=_PRODUCT_DESC)
    amount: float = Field(description="Amount to consume, in `qu` units.")
    qu: int | str = Field(description=_QU_DESC)
    location: int | str = Field(
        description="Location to consume from. Name or ID. Use `stock_get` first if you don't know where the product is."
    )
    spoiled: bool = Field(default=False, description="Mark the consumption as spoilage rather than normal use.")
    allow_subproduct_substitution: bool = Field(
        default=False,
        description="If the product has sub-products configured, allow Grocy to consume from sub-product stock when the product itself is short.",
    )


class SetItem(BaseModel):
    """One absolute-amount correction for `stock_set`."""

    product: int | str = Field(description=_PRODUCT_DESC)
    new_amount: float = Field(
        description="Absolute target stock amount in `qu` units. Grocy computes the delta and adds or removes to reach it."
    )
    qu: int | str = Field(description=_QU_DESC)
    location: int | str = Field(description="Location for any units being added by the correction. Name or ID.")
    best_before_date: date | None = Field(
        default=None, description=f"Applies to units being added: {_BEST_BEFORE_DESC.lower()}"
    )
    price: float | None = Field(
        default=None, description="Per-unit price for units being added. Omit to use the last recorded price."
    )


class StockOpOk(BaseModel):
    """Per-item success returned by `stock_add` / `stock_consume` / `stock_set` / `stock_transfer`."""

    kind: Literal["ok"] = "ok"
    product_name: str
    transaction_id: str | None = Field(
        default=None, description="Grocy transaction ID. Pass to `transaction_undo` to revert this single op."
    )
    amount_delta: float | None = Field(
        default=None, description="Net stock change in stock QU. Negative for consume; null for transfer."
    )
    new_amount: float | None = Field(
        default=None,
        description="Resulting total stock for this product, in stock QU. Best-effort: null if the follow-up read fails.",
    )
    qu_name: str = Field(description="Name of the quantity unit the input `amount` was specified in.")
    stock_qu_name: str | None = Field(
        default=None,
        description=(
            "Name of the product's stock QU when a conversion was applied. Null on a normal success "
            "where the input `qu` already matched the product's stock QU (no conversion needed)."
        ),
    )
    location_name: str = Field(
        description="Name of the storage location. For `stock_transfer`, formatted as `from -> to`."
    )


class StockOpError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


# ── Stock entry input/output ─────────────────────────────────────────────────


class StockEntryDetail(BaseModel):
    """Detailed stock entry with names."""

    entry_id: int
    product_id: int
    product_name: str
    amount: float
    qu_name: str
    location_name: str
    best_before_date: date | None = None
    purchased_date: date | None = None
    price: float | None = None
    open: bool = False
    note: str | None = None


class StockEntryOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entry: StockEntryDetail
    changes: dict[str, dict[str, Any]] | None = Field(
        default=None, description="For edits: {field: {old: ..., new: ...}} diff of changed fields."
    )


class StockEntryError(BaseModel):
    kind: Literal["error"] = "error"
    entry_id: int | None = None
    error: str


class EditStockEntryField(StrEnum):
    """Nullable stock-entry fields that `stock_entry_edit.clear_fields` can null out.

    Other stock-entry fields (`amount`, `location_id`, `open`) are NOT
    NULL in Grocy's schema and must be assigned a value if changed.
    """

    PRICE = "price"
    BEST_BEFORE_DATE = "best_before_date"
    PURCHASED_DATE = "purchased_date"
    NOTE = "note"


# ── Reference data create types ──────────────────────────────────────────────


class CreateProductItem(BaseModel):
    """One product to create via `products_create`."""

    name: str = Field(description="Product name. Must be unique across all products.")
    stock_qu: int | str = Field(description="Stock quantity unit. Name or ID. The unit Grocy stores stock totals in.")
    location: int | str = Field(description="Default storage location. Name or ID.")
    purchase_qu: int | str | None = Field(
        default=None, description="Purchase quantity unit. Name or ID. Defaults to `stock_qu`."
    )
    min_stock_amount: float = Field(
        default=0,
        description="Threshold (in stock QU) below which `get_below_minimum_stock` flags this product. 0 disables.",
    )
    default_best_before_days: int = Field(
        default=0, description="Auto-fill best-before date this many days ahead when `stock_add` omits it. 0 disables."
    )
    product_group: int | str | None = Field(default=None, description="Product group / category. Name or ID.")
    description: str | None = Field(default=None, description="Free-text description.")


class CreateLocationItem(BaseModel):
    """One storage location to create via `locations_create`."""

    name: str = Field(description="Location name (e.g. Pantry, Fridge, Freezer). Must be unique across all locations.")
    description: str | None = Field(default=None, description="Free-text description.")
    is_freezer: bool = Field(
        default=False,
        description=(
            "True if this is a freezer. Grocy applies frozen/thawed best-before-days adjustments to "
            "stock entries moved into or out of freezer locations."
        ),
    )


class CreateQuantityUnitItem(BaseModel):
    """One quantity unit to create via `quantity_units_create`."""

    name: str = Field(description="Singular form (e.g. Liter, Bag, Piece). Must be unique across all units.")
    name_plural: str = Field(description="Plural form (e.g. Liters, Bags, Pieces).")
    description: str | None = Field(default=None, description="Free-text description.")
    plural_forms: str | None = Field(
        default=None,
        description="Optional Gettext-style plural rules for non-English locales (e.g. `nplurals=3; plural=…;`).",
    )


class CreateShoppingListItem(BaseModel):
    """One shopping-list metadata row to create via `shopping_lists_create`.

    The list itself, not an item on a list — individual items go through
    `shopping_list_items_add`.
    """

    name: str = Field(description="List name (e.g. Weekly, Costco run). Must be unique across all shopping lists.")
    description: str | None = Field(default=None, description="Free-text description.")


class CreateProductGroupItem(BaseModel):
    """One product group (category) to create via `product_groups_create`."""

    name: str = Field(description="Group name (e.g. Dairy, Produce). Must be unique across all product groups.")
    description: str | None = Field(default=None, description="Free-text description.")


# ── Product edit types ────────────────────────────────────────────────────────


class EditProductField(StrEnum):
    """Nullable product fields that `product_edit.clear_fields` can null out.

    Other product fields are NOT NULL in Grocy's schema and cannot be cleared
    — to remove a product entirely, use `product_delete`.
    """

    DESCRIPTION = "description"
    PRODUCT_GROUP = "product_group"
    PARENT_PRODUCT = "parent_product"
    CALORIES = "calories"


# Writable columns on the products table (migration 0207 + 0210 + 0219).
# Grocy's GET returns computed view fields (has_sub_products, qu_factor_*, etc.)
# that are rejected on PUT. Only send these columns.
_PRODUCT_WRITABLE_FIELDS: set[str] = {
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


# ── Shopping list types ──────────────────────────────────────────────────────


class ShoppingItem(BaseModel):
    """One shopping-list item to add via `shopping_list_items_add`.

    Three valid shapes: product-only (`product` set), note-only (`note`
    set), or product + note (e.g. "Milk — buy the organic brand"). At
    least one of the two must be provided.
    """

    shopping_list: int | str = Field(description="Target shopping list. Name or ID.")
    product: int | str | None = Field(default=None, description="Product to add. Name or ID. Omit for note-only items.")
    amount: float = Field(default=1, description="Quantity to buy, in the product's stock QU.")
    note: str | None = Field(
        default=None,
        description="Free-text note. May accompany a `product` to qualify it, or stand alone for note-only items.",
    )

    @model_validator(mode="after")
    def _at_least_one_of_product_or_note(self) -> ShoppingItem:
        if self.product is None and self.note is None:
            raise ValueError("ShoppingItem requires at least one of `product` or `note`.")
        return self


class ShoppingListItemOk(BaseModel):
    """Per-item success returned by every shopping-list mutation tool."""

    kind: Literal["ok"] = "ok"
    item_id: int = Field(description="Grocy shopping-list item ID.")
    product_name: str | None = Field(default=None, description="Product name; null for note-only items.")
    amount: float
    qu_name: str | None = Field(default=None, description="Stock QU name; null for note-only items.")


class ShoppingListItemError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class EditShoppingListField(StrEnum):
    """Nullable shopping-list-item fields that `shopping_list_item_edit.clear_fields` can null out.

    Other fields (`amount`, `done`, `shopping_list_id`) are NOT NULL in
    Grocy's schema. To re-point an item at a different product, remove
    it via `shopping_list_items_remove` and re-add via
    `shopping_list_items_add`.
    """

    NOTE = "note"


# ── Reference-data list result shapes ────────────────────────────────────────


class BriefListItem(BaseModel):
    """Minimal `list_*` row returned when `detail="brief"`: just `id` + `name`."""

    id: int
    name: str


class BriefQuantityUnit(BriefListItem):
    """Brief QU entry; adds `name_plural` since it's frequently needed at call sites."""

    name_plural: str | None = None


class FullProduct(BaseModel):
    """Full product row — Grocy's schema varies by version, so unlisted columns are passed through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullLocation(BaseModel):
    """Full location row — `is_freezer`, description, and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullQuantityUnit(BaseModel):
    """Full QU row — `name_plural`, description, and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str
    name_plural: str | None = None


class FullProductGroup(BaseModel):
    """Full product-group row — description and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullShoppingList(BaseModel):
    """Full shopping-list metadata row — description and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


# ── Tool registration ────────────────────────────────────────────────────────


def register_batch_tools(mcp: FastMCP, client: httpx.AsyncClient, settings: ServerSettings) -> None:
    """Register custom batch tools on an existing FastMCP instance."""
    sem = asyncio.Semaphore(settings.max_concurrent_requests)
    max_batch = settings.max_batch_size
    max_retries = settings.max_retries
    base_delay = settings.retry_base_delay
    # One stateless resolver shared by every tool — saves the
    # `EntityResolver(client)` boilerplate at each call site. Caching is
    # intentionally absent (see resolver.py): the MCP server isn't the
    # only client of a Grocy instance, so any cache could go stale
    # behind us.
    resolver = EntityResolver(client)

    def _check_batch_size(items: list[Any] | set[Any], label: str) -> None:
        if len(items) > max_batch:
            raise ValueError(f"batch too large: {len(items)} {label} exceeds maximum of {max_batch}")

    async def _retry[T](fn: Callable[[], Awaitable[T]]) -> T:
        return await _with_retry(fn, sem, max_retries=max_retries, base_delay=base_delay)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _build_enrichment_maps() -> tuple[dict[int, dict[str, Any]], dict[int, str], dict[int, str]]:
        """One-shot reference-data fetch for `_enrich_stock_entry`.

        Each name/field lookup would otherwise re-fetch `/objects/<entity>`,
        which becomes O(rows * 4) when iterating through many stock entries.
        Caller fetches once, then calls `_enrich_stock_entry` in a loop with
        these maps — state can't mutate mid-call so the local maps are safe.
        """
        products, qus, locations = await asyncio.gather(
            resolver.all_products(), resolver.all_qus(), resolver.all_locations()
        )
        return (
            {int(p["id"]): p for p in products},
            {int(q["id"]): str(q["name"]) for q in qus},
            {int(row["id"]): str(row["name"]) for row in locations},
        )

    async def _enrich_stock_entry(
        entry_data: dict[str, Any],
        *,
        products_by_id: dict[int, dict[str, Any]] | None = None,
        qu_names: dict[int, str] | None = None,
        location_names: dict[int, str] | None = None,
    ) -> StockEntryDetail:
        """Convert a raw Grocy stock entry dict to a StockEntryDetail with names.

        Pass the maps from `_build_enrichment_maps` when enriching in a loop
        to avoid per-row fetches. When called without maps, builds them here
        (one fetch of each reference table for the single entry).
        """
        if products_by_id is None or qu_names is None or location_names is None:
            products_by_id, qu_names, location_names = await _build_enrichment_maps()

        product_id = int(entry_data["product_id"])
        product = products_by_id.get(product_id)
        product_name = str(product["name"]) if product else f"id={product_id}"
        stock_qu_id = int(product["qu_id_stock"]) if product else 0
        qu_name = qu_names.get(stock_qu_id, f"id={stock_qu_id}")
        loc_id = entry_data.get("location_id")
        if loc_id is not None:
            location_name = location_names.get(int(loc_id), f"id={loc_id}")
        else:
            # Fall back to product's default location
            default_loc = int(product["location_id"]) if product else 0
            location_name = location_names.get(default_loc, f"id={default_loc}")
        return StockEntryDetail(
            entry_id=int(entry_data["id"]),
            product_id=product_id,
            product_name=product_name,
            amount=float(entry_data["amount"]),
            qu_name=qu_name,
            location_name=location_name,
            best_before_date=_parse_date(entry_data.get("best_before_date")),
            purchased_date=_parse_date(entry_data.get("purchased_date")),
            price=float(entry_data["price"]) if entry_data.get("price") is not None else None,
            open=entry_data.get("open") in (True, 1, "1"),
            note=entry_data.get("note"),
        )

    # ── Entity CRUD ──────────────────────────────────────────────────────

    @mcp.tool()
    async def entities_create(items: list[CreateItem]) -> list[CreateOk | CreateError]:
        """Create entities of any writeable type. Max 20 items per call.

        For products, locations, and quantity units prefer the typed
        `products_create`, `locations_create`, `quantity_units_create` —
        they take named fields and resolve `name | id` references for you.
        Use this tool for the rest of the writeable surface
        (`shopping_lists`, `recipes`, `tasks`, …); see `WriteableEntityType`
        for the full set.

        Each item is `{entity_type, body}` where `body` is a dict of that
        entity's columns. Failed items return errors without aborting the
        others; successes return `created_object_id`.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateItem) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    r = await client.post(f"/objects/{item.entity_type}", json=item.body)
                    r.raise_for_status()
                    data = r.json()
                    raw_id = data.get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def entities_list(entity_types: list[ReadableEntityType]) -> dict[str, list[Any]]:
        """Fetch every object of one or more entity types. Max 20 types per call.

        For the common reference types prefer the dedicated `products_list`,
        `locations_list`, `quantity_units_list`, `product_groups_list` —
        they support a `brief` mode that returns just `{id, name}` pairs.

        Use this for less common types (`product_barcodes`, `shopping_lists`,
        `quantity_unit_conversions`, …) and for the read-only views
        (`stock`, `stock_log`, `products_last_purchased`, etc.) — see
        `ReadableEntityType` for the full set.

        Returns `{entity_type: [objects, …]}`.
        """
        _check_batch_size(entity_types, "entity_types")

        async def _fetch(entity_type: ReadableEntityType) -> tuple[str, list[Any]]:
            async def _do() -> tuple[str, list[Any]]:
                r = await client.get(f"/objects/{entity_type}")
                r.raise_for_status()
                return str(entity_type), r.json()

            return await _retry(_do)

        pairs = await asyncio.gather(*[_fetch(et) for et in entity_types])
        return dict(pairs)

    @mcp.tool()
    async def entities_get(entity_type: ReadableEntityType, object_ids: list[int]) -> list[GetOk | GetError]:
        """Fetch specific objects by ID for one entity type. Max 20 IDs per call.

        Use this when you already know the IDs (e.g. from `entities_list`,
        `products_list`, or a previous `create_*` call). Each result carries
        `entity_type` and `object_id` so you can match it back. Failed
        fetches return errors without aborting the others.
        """
        _check_batch_size(object_ids, "object_ids")

        async def _one(object_id: int) -> GetOk | GetError:
            try:

                async def _do() -> GetOk:
                    r = await client.get(f"/objects/{entity_type}/{object_id}")
                    r.raise_for_status()
                    return GetOk(entity_type=entity_type, object_id=object_id, data=r.json())

                return await _retry(_do)
            except Exception as e:
                return GetError(entity_type=entity_type, object_id=object_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_one(oid) for oid in object_ids]))

    # ── Stock overview ───────────────────────────────────────────────────

    @mcp.tool()
    async def stock_get(
        products: Annotated[
            list[int | str],
            Field(
                default_factory=list,
                description="Products (name or ID) to restrict to. Empty list = no product filter.",
            ),
        ],
        locations: Annotated[
            list[int | str],
            Field(
                default_factory=list,
                description="Locations (name or ID) to restrict to. Empty list = no location filter.",
            ),
        ],
    ) -> list[StockEntry]:
        """Aggregate stock-on-hand: product, amount, QU, location, best-before.

        Answers "what's in stock?" Filters AND together; pass empty lists
        (the default) for the whole inventory. Use this before
        `stock_consume` / `stock_set` to find which `location` to pass
        when the same product lives in more than one. For per-stock-entry
        detail (line items, prices, dates), use `stock_entries_list`
        instead.
        """

        r = await _retry(lambda: client.get("/stock"))
        r.raise_for_status()
        stock_data: list[dict[str, Any]] = r.json()

        product_ids: set[int] = set()
        for ref in products:
            resolved = await resolver.resolve_product(ref)
            product_ids.add(resolved.id)

        location_ids: set[int] = set()
        for ref in locations:
            resolved = await resolver.resolve_location(ref)
            location_ids.add(resolved.id)

        result = []
        for entry in stock_data:
            pid = int(entry["product_id"])
            if product_ids and pid not in product_ids:
                continue

            product = entry.get("product") or {}
            loc_id_raw = product.get("location_id")
            loc_id = int(loc_id_raw) if loc_id_raw is not None else None

            if location_ids and (loc_id is None or loc_id not in location_ids):
                continue

            qu_id_raw = product.get("qu_id_stock")
            qu_name = await resolver.qu_name(int(qu_id_raw)) if qu_id_raw is not None else "unknown"
            location_name = await resolver.location_name(loc_id) if loc_id is not None else "unknown"
            product_name = str(product.get("name", f"product_id={pid}"))

            result.append(
                StockEntry(
                    product_id=pid,
                    product_name=product_name,
                    amount=float(entry["amount"]),
                    amount_opened=float(entry["amount_opened"]),
                    qu_name=qu_name,
                    location_name=location_name,
                    best_before_date=_parse_date(entry.get("best_before_date")),
                )
            )
        return result

    # ── Stock mutations ──────────────────────────────────────────────────

    async def _best_effort_new_amount(product_id: int) -> float | None:
        """Read current stock amount after a mutation. Best-effort, never retried."""
        try:
            stock_r = await client.get(f"/stock/products/{product_id}")
            stock_r.raise_for_status()
            return float(stock_r.json().get("stock_amount", 0))
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            logger.warning("failed to read new_amount for product %d after mutation", product_id)
            return None

    @mcp.tool()
    async def stock_add(items: list[AddItem]) -> list[StockOpOk | StockOpError]:
        """Add stock for one or more products. Max 20 items per call.

        Each item needs `product`, `amount`, `qu`, and `location`. The `qu`
        must match the product's stock QU or have a defined conversion
        (e.g. "Crate" → "Bottle"); see `quantity_unit_conversions` via
        `entities_list`.

        Returns one result per input item, in order. Each success carries
        a `transaction_id` you can pass to `transaction_undo` to revert
        that single addition. See also `stock_consume`,
        `stock_set`, `stock_transfer`.
        """
        _check_batch_size(items, "items")

        async def _one(item: AddItem) -> StockOpOk | StockOpError:
            try:
                product = await resolver.resolve_product(item.product)
                location = await resolver.resolve_location(item.location)
                rqu = await resolver.resolve_qu_for_product(item.qu, product.id)

                stock_amount = item.amount * rqu.conversion_factor

                async def _do_post() -> tuple[str | None, float | None]:
                    body: dict[str, Any] = {"amount": stock_amount}
                    if item.best_before_date is not None:
                        body["best_before_date"] = _date_to_str(item.best_before_date)
                    if item.price is not None:
                        body["price"] = item.price
                    body["location_id"] = location.id
                    if item.note is not None:
                        body["note"] = item.note
                    r = await client.post(f"/stock/products/{product.id}/add", json=body)
                    r.raise_for_status()
                    entries: list[dict[str, Any]] = r.json()
                    tx_id = entries[0]["transaction_id"] if entries else None
                    amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(product.id)
                return StockOpOk(
                    product_name=product.name,
                    transaction_id=tx_id,
                    amount_delta=amount_delta,
                    new_amount=new_amount,
                    qu_name=rqu.name,
                    stock_qu_name=rqu.stock_qu_name if rqu.conversion_factor != 1.0 else None,
                    location_name=location.name,
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def stock_consume(items: list[ConsumeItem]) -> list[StockOpOk | StockOpError]:
        """Consume (use up) stock for one or more products. Max 20 items per call.

        Use `stock_get` first if you don't already know which `location`
        the product is in — Grocy can hold the same product in multiple
        locations, and consuming from the wrong one silently picks the
        first match. See also `stock_add`, `stock_set`,
        `stock_transfer`. Each success carries a `transaction_id` for
        `transaction_undo`.
        """
        _check_batch_size(items, "items")

        async def _one(item: ConsumeItem) -> StockOpOk | StockOpError:
            try:
                product = await resolver.resolve_product(item.product)
                location = await resolver.resolve_location(item.location)
                rqu = await resolver.resolve_qu_for_product(item.qu, product.id)

                stock_amount = item.amount * rqu.conversion_factor

                async def _do_post() -> tuple[str | None, float | None]:
                    body: dict[str, Any] = {
                        "amount": stock_amount,
                        "spoiled": item.spoiled,
                        "allow_subproduct_substitution": item.allow_subproduct_substitution,
                        "location_id": location.id,
                    }
                    r = await client.post(f"/stock/products/{product.id}/consume", json=body)
                    r.raise_for_status()
                    entries: list[dict[str, Any]] = r.json()
                    tx_id = entries[0]["transaction_id"] if entries else None
                    amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(product.id)
                return StockOpOk(
                    product_name=product.name,
                    transaction_id=tx_id,
                    amount_delta=amount_delta,
                    new_amount=new_amount,
                    qu_name=rqu.name,
                    stock_qu_name=rqu.stock_qu_name if rqu.conversion_factor != 1.0 else None,
                    location_name=location.name,
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def stock_set(items: list[SetItem]) -> list[StockOpOk | StockOpError]:
        """Set absolute stock amounts for one or more products. Max 20 items per call.

        Use this for corrections — "we actually have 10 kg of rice, not 3"
        — when you'd otherwise have to compute the delta yourself for
        `stock_add` / `stock_consume`. Grocy figures out whether to add
        or remove to reach `new_amount`. `best_before_date` and `price`
        apply only to units being added; ignored when removing.
        Each success carries a `transaction_id` for `transaction_undo`.
        See also `stock_add`, `stock_consume`, `stock_transfer`.
        """
        _check_batch_size(items, "items")

        async def _one(item: SetItem) -> StockOpOk | StockOpError:
            try:
                product = await resolver.resolve_product(item.product)
                location = await resolver.resolve_location(item.location)
                rqu = await resolver.resolve_qu_for_product(item.qu, product.id)

                stock_amount = item.new_amount * rqu.conversion_factor

                async def _do_post() -> tuple[str | None, float | None]:
                    body: dict[str, Any] = {"new_amount": stock_amount, "location_id": location.id}
                    if item.best_before_date is not None:
                        body["best_before_date"] = _date_to_str(item.best_before_date)
                    if item.price is not None:
                        body["price"] = item.price
                    r = await client.post(f"/stock/products/{product.id}/inventory", json=body)
                    r.raise_for_status()
                    entries: list[dict[str, Any]] = r.json()
                    tx_id = entries[0]["transaction_id"] if entries else None
                    amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(product.id)
                return StockOpOk(
                    product_name=product.name,
                    transaction_id=tx_id,
                    amount_delta=amount_delta,
                    new_amount=new_amount,
                    qu_name=rqu.name,
                    stock_qu_name=rqu.stock_qu_name if rqu.conversion_factor != 1.0 else None,
                    location_name=location.name,
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    # ── Stock entry read/edit ────────────────────────────────────────────

    @mcp.tool()
    async def stock_entries_list(
        products: Annotated[
            list[int | str] | None, Field(description="Products (name or ID) — returns every entry for each.")
        ] = None,
        entry_ids: Annotated[list[int] | None, Field(description="Specific stock-entry IDs to fetch.")] = None,
    ) -> list[StockEntryOk | StockEntryError]:
        """Fetch individual stock entries — the line items that make up a product's stock.

        Provide exactly one of `products` or `entry_ids`:

        - `products`: every stock entry for each product (by name or ID),
          with dates, prices, and locations. Use for the "what's in
          stock, broken down by purchase" view.
        - `entry_ids`: specific entries by ID — typically obtained from
          a previous `stock_entries_list(products=…)` call so you can
          pass the IDs to `stock_entry_edit`.
        """
        if (products is None) == (entry_ids is None):
            raise ValueError("Provide exactly one of 'products' or 'entry_ids', not both or neither.")

        if products is not None:
            _check_batch_size(products, "products")
            products_by_id, qu_names, location_names = await _build_enrichment_maps()
            all_results: list[StockEntryOk | StockEntryError] = []
            for product_ref in products:
                resolved = await resolver.resolve_product(product_ref)
                try:
                    r = await _retry(functools.partial(client.get, f"/stock/products/{resolved.id}/entries"))
                    r.raise_for_status()
                    entries_data: list[dict[str, Any]] = r.json()
                    for entry_data in entries_data:
                        try:
                            detail = await _enrich_stock_entry(
                                entry_data,
                                products_by_id=products_by_id,
                                qu_names=qu_names,
                                location_names=location_names,
                            )
                            all_results.append(StockEntryOk(entry=detail))
                        except Exception as e:
                            all_results.append(
                                StockEntryError(entry_id=int(entry_data.get("id", 0)), error=_format_exc(e))
                            )
                except Exception as e:
                    all_results.append(StockEntryError(error=_format_exc(e)))
            return all_results

        # Entry ID mode: batch GET /stock/entry/{id}
        assert entry_ids is not None
        _check_batch_size(entry_ids, "entry_ids")

        async def _one(entry_id: int) -> StockEntryOk | StockEntryError:
            try:

                async def _do() -> StockEntryOk:
                    r = await client.get(f"/stock/entry/{entry_id}")
                    r.raise_for_status()
                    detail = await _enrich_stock_entry(r.json())
                    return StockEntryOk(entry=detail)

                return await _retry(_do)
            except Exception as e:
                return StockEntryError(entry_id=entry_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_one(eid) for eid in entry_ids]))

    @mcp.tool()
    async def stock_entry_edit(
        entry_id: Annotated[int, Field(description="Stock-entry ID (from `stock_entries_list`).")],
        amount: Annotated[float | None, Field(description="New amount, in the entry's stock QU.")] = None,
        best_before_date: Annotated[date | None, Field(description=f"New {_BEST_BEFORE_DESC.lower()}")] = None,
        purchased_date: Annotated[date | None, Field(description="New purchase date in `YYYY-MM-DD` format.")] = None,
        price: Annotated[float | None, Field(description="New per-unit price.")] = None,
        location: Annotated[int | str | None, Field(description="New storage location. Name or ID.")] = None,
        open: Annotated[bool | None, Field(description="Mark the entry as opened or unopened.")] = None,
        note: Annotated[str | None, Field(description="New free-text note.")] = None,
        clear_fields: Annotated[
            set[EditStockEntryField] | None,
            Field(
                description=(
                    "Fields to explicitly null out. Only the values in `EditStockEntryField` are nullable in "
                    "Grocy's schema; everything else is NOT NULL and must be assigned a value if changed."
                )
            ),
        ] = None,
    ) -> StockEntryOk | StockEntryError:
        """Partial update of a stock entry — only the fields you pass change.

        The server reads the current entry, merges your changes, and
        writes back, so you don't have to copy-paste unchanged fields.
        Returns the post-edit entry plus a `changes` diff. To remove a
        nullable field's value (vs setting it to a new value), name it
        in `clear_fields`. See also `stock_entries_list` to discover IDs.
        """

        try:
            # Read current entry
            entry_r = await client.get(f"/stock/entry/{entry_id}")
            entry_r.raise_for_status()
            current: dict[str, Any] = entry_r.json()

            # Build merged body
            body: dict[str, Any] = {
                "amount": current.get("amount"),
                "best_before_date": current.get("best_before_date"),
                "purchased_date": current.get("purchased_date"),
                "price": current.get("price"),
                "location_id": current.get("location_id"),
                "open": current.get("open") in (True, 1, "1"),
                "note": current.get("note"),
            }

            # Apply explicit changes
            if amount is not None:
                body["amount"] = amount
            if best_before_date is not None:
                body["best_before_date"] = _date_to_str(best_before_date)
            if purchased_date is not None:
                body["purchased_date"] = _date_to_str(purchased_date)
            if price is not None:
                body["price"] = price
            if location is not None:
                resolved_loc = await resolver.resolve_location(location)
                body["location_id"] = resolved_loc.id
            if open is not None:
                body["open"] = open
            if note is not None:
                body["note"] = note

            # Apply clears
            if clear_fields:
                for field_name in clear_fields:
                    if field_name == EditStockEntryField.PRICE:
                        body["price"] = None
                    elif field_name == EditStockEntryField.BEST_BEFORE_DATE:
                        body["best_before_date"] = None
                    elif field_name == EditStockEntryField.PURCHASED_DATE:
                        body["purchased_date"] = None
                    elif field_name == EditStockEntryField.NOTE:
                        body["note"] = None

            # Compute diff before writing
            diff_fields = {"amount", "best_before_date", "purchased_date", "price", "location_id", "open", "note"}
            changes: dict[str, dict[str, Any]] = {}
            for field in diff_fields:
                old_val = current.get(field)
                new_val = body.get(field)
                if old_val != new_val:
                    changes[field] = {"old": old_val, "new": new_val}

            # Write back
            r = await client.put(f"/stock/entry/{entry_id}", json=body)
            r.raise_for_status()

            # Re-fetch to return updated state
            updated_r = await client.get(f"/stock/entry/{entry_id}")
            updated_r.raise_for_status()
            detail = await _enrich_stock_entry(updated_r.json())
            return StockEntryOk(entry=detail, changes=changes or None)
        except Exception as e:
            return StockEntryError(entry_id=entry_id, error=_format_exc(e))

    # ── Reference data ───────────────────────────────────────────────────

    @mcp.tool()
    async def products_list(
        detail: Annotated[Literal["brief", "full"], Field(description=_DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullProduct]:
        """Returns every product defined in this Grocy instance. Create new ones with `products_create`.

        Most tools accept product names directly, so you usually only
        need this when you want the full catalogue or the `full` shape
        (default location, stock QU, etc.).
        """
        rows = await resolver.all_products()
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullProduct.model_validate(r) for r in rows]

    @mcp.tool()
    async def locations_list(
        detail: Annotated[Literal["brief", "full"], Field(description=_DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullLocation]:
        """Returns every storage location defined in this Grocy instance. Create new ones with `locations_create`.

        Stock operations accept location names directly; use this when
        you want to see what already exists before `products_create` or
        `stock_add`.
        """
        rows = await resolver.all_locations()
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullLocation.model_validate(r) for r in rows]

    @mcp.tool()
    async def quantity_units_list(
        detail: Annotated[Literal["brief", "full"], Field(description=_DETAIL_DESC)] = "brief",
    ) -> list[BriefQuantityUnit] | list[FullQuantityUnit]:
        """Returns every quantity unit defined in this Grocy instance. Create new ones with `quantity_units_create`.

        Grocy ships with only `Piece` pre-defined; any unit the agent
        needs (Kilogram, Liter, Bag, …) has to be created first. Every
        stock operation needs a `qu`; check here when in doubt. For
        conversions between units, list `quantity_unit_conversions` via
        `entities_list`.
        """
        rows = await resolver.all_qus()
        if detail == "brief":
            return [
                BriefQuantityUnit(id=int(r["id"]), name=str(r["name"]), name_plural=r.get("name_plural")) for r in rows
            ]
        return [FullQuantityUnit.model_validate(r) for r in rows]

    @mcp.tool()
    async def product_groups_list(
        detail: Annotated[Literal["brief", "full"], Field(description=_DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullProductGroup]:
        """Returns every product-group (category) defined in this Grocy instance.

        Pass product-group names or IDs to `products_create` /
        `product_edit`. Create new ones with `product_groups_create`.
        """
        rows = await resolver.all_product_groups()
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullProductGroup.model_validate(r) for r in rows]

    @mcp.tool()
    async def shopping_lists_list(
        detail: Annotated[Literal["brief", "full"], Field(description=_DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullShoppingList]:
        """Returns every shopping list defined in this Grocy instance.

        This is the list-metadata table (one row per named list like
        "Weekly" or "Costco run"), *not* the items on those lists —
        for items use `shopping_list_get`. Create new lists with
        `shopping_lists_create`.
        """
        r = await _retry(lambda: client.get("/objects/shopping_lists"))
        r.raise_for_status()
        rows: list[dict[str, Any]] = r.json()
        if detail == "brief":
            return [BriefListItem(id=int(row["id"]), name=str(row["name"])) for row in rows]
        return [FullShoppingList.model_validate(row) for row in rows]

    # ── Reference data creation ──────────────────────────────────────────

    @mcp.tool()
    async def locations_create(items: list[CreateLocationItem]) -> list[CreateOk | CreateError]:
        """Create one or more storage locations. Max 20 items per call.

        Locations are referenced by name everywhere else (stock ops,
        product creation, etc.) — pick stable, distinctive names. Use
        `locations_list` to discover what already exists. Failed items
        return errors without aborting the others.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateLocationItem) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    body: dict[str, Any] = {"name": item.name, "is_freezer": int(item.is_freezer)}
                    if item.description is not None:
                        body["description"] = item.description
                    r = await client.post("/objects/locations", json=body)
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def quantity_units_create(items: list[CreateQuantityUnitItem]) -> list[CreateOk | CreateError]:
        """Create one or more quantity units. Max 20 items per call.

        Quantity units are referenced by name in stock ops and product
        creation; the singular `name` is what stock operations match
        against. Use `quantity_units_list` to discover what already
        exists. To make units interconvertible (e.g. 1 Pack = 6 Bottles),
        create entries in `quantity_unit_conversions` via `entities_create`
        afterwards. Failed items return errors without aborting the others.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateQuantityUnitItem) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    body: dict[str, Any] = {"name": item.name, "name_plural": item.name_plural}
                    if item.description is not None:
                        body["description"] = item.description
                    if item.plural_forms is not None:
                        body["plural_forms"] = item.plural_forms
                    r = await client.post("/objects/quantity_units", json=body)
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def product_groups_create(items: list[CreateProductGroupItem]) -> list[CreateOk | CreateError]:
        """Create one or more product groups (categories). Max 20 items per call.

        Categorising products is optional in Grocy; these groups show up as
        the `product_group` reference on products. Use `product_groups_list`
        to discover existing groups. Failed items return errors without
        aborting the others.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateProductGroupItem) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    body: dict[str, Any] = {"name": item.name}
                    if item.description is not None:
                        body["description"] = item.description
                    r = await client.post("/objects/product_groups", json=body)
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def shopping_lists_create(items: list[CreateShoppingListItem]) -> list[CreateOk | CreateError]:
        """Create one or more shopping lists (metadata). Max 20 items per call.

        This creates the list itself, not items on it — items go through
        `shopping_list_items_add`. Pass the returned list name or ID as
        the `shopping_list` argument to every shopping-list tool. Use
        `shopping_lists_list` to discover existing lists. Failed items
        return errors without aborting the others.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateShoppingListItem) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    body: dict[str, Any] = {"name": item.name}
                    if item.description is not None:
                        body["description"] = item.description
                    r = await client.post("/objects/shopping_lists", json=body)
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    # ── Product management ───────────────────────────────────────────────

    @mcp.tool()
    async def products_create(items: list[CreateProductItem]) -> list[CreateOk | CreateError]:
        """Create one or more products. Max 20 items per call.

        Each item needs `name`, `stock_qu`, and `location`. All entity
        references (`stock_qu`, `location`, `purchase_qu`, `product_group`)
        take names or IDs and resolve via `quantity_units_list` /
        `locations_list` / `product_groups_list`. `purchase_qu` defaults to
        `stock_qu` when omitted. Failed items return errors without
        aborting the others.

        Pair with `locations_create` and `quantity_units_create` to bring
        up a fresh Grocy instance from scratch; use `product_edit` /
        `product_delete` for mutations after creation.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateProductItem) -> CreateOk | CreateError:
            try:
                loc = await resolver.resolve_location(item.location)
                squ = await resolver.resolve_qu(item.stock_qu)
                pqu = await resolver.resolve_qu(item.purchase_qu) if item.purchase_qu is not None else squ

                body: dict[str, Any] = {
                    "name": item.name,
                    "location_id": loc.id,
                    "qu_id_stock": squ.id,
                    "qu_id_purchase": pqu.id,
                    "min_stock_amount": item.min_stock_amount,
                    "default_best_before_days": item.default_best_before_days,
                }
                if item.product_group is not None:
                    pg = await resolver.resolve_product_group(item.product_group)
                    body["product_group_id"] = pg.id
                if item.description is not None:
                    body["description"] = item.description

                async def _do() -> CreateOk:
                    r = await client.post("/objects/products", json=body)
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def product_edit(
        product: Annotated[int | str, Field(description="Product to edit. Name or ID.")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        stock_qu: Annotated[int | str | None, Field(description="New stock quantity unit. Name or ID.")] = None,
        location: Annotated[int | str | None, Field(description="New default storage location. Name or ID.")] = None,
        purchase_qu: Annotated[int | str | None, Field(description="New purchase quantity unit. Name or ID.")] = None,
        min_stock_amount: Annotated[
            float | None, Field(description="New low-stock threshold (in stock QU). 0 disables.")
        ] = None,
        default_best_before_days: Annotated[
            int | None, Field(description="New auto-fill best-before days for `stock_add`. 0 disables.")
        ] = None,
        product_group: Annotated[int | str | None, Field(description="New product group. Name or ID.")] = None,
        description: Annotated[str | None, Field(description="New free-text description.")] = None,
        clear_fields: Annotated[
            set[EditProductField] | None,
            Field(
                description=(
                    "Fields to explicitly null out. Only the values in `EditProductField` are nullable in "
                    "Grocy's schema; everything else is NOT NULL."
                )
            ),
        ] = None,
    ) -> CreateOk | CreateError:
        """Partial update of a product — only the fields you pass change.

        The server reads the current product, merges your changes, and
        writes back. To remove a nullable field's value (vs setting it
        to a new one), name it in `clear_fields`. See also
        `products_create`, `product_delete`, `products_list`.
        """
        try:
            resolved = await resolver.resolve_product(product)
            r = await client.get(f"/objects/products/{resolved.id}")
            r.raise_for_status()
            current: dict[str, Any] = r.json()

            # Filter to writable columns — Grocy's GET returns computed view
            # fields that are rejected on PUT.
            body = {k: v for k, v in current.items() if k in _PRODUCT_WRITABLE_FIELDS}
            if name is not None:
                body["name"] = name
            if stock_qu is not None:
                body["qu_id_stock"] = (await resolver.resolve_qu(stock_qu)).id
            if location is not None:
                body["location_id"] = (await resolver.resolve_location(location)).id
            if purchase_qu is not None:
                body["qu_id_purchase"] = (await resolver.resolve_qu(purchase_qu)).id
            if min_stock_amount is not None:
                body["min_stock_amount"] = min_stock_amount
            if default_best_before_days is not None:
                body["default_best_before_days"] = default_best_before_days
            if product_group is not None:
                body["product_group_id"] = (await resolver.resolve_product_group(product_group)).id
            if description is not None:
                body["description"] = description

            if clear_fields:
                for field in clear_fields:
                    if field == EditProductField.DESCRIPTION:
                        body["description"] = None
                    elif field == EditProductField.PRODUCT_GROUP:
                        body["product_group_id"] = None
                    elif field == EditProductField.PARENT_PRODUCT:
                        body["parent_product_id"] = None
                    elif field == EditProductField.CALORIES:
                        body["calories"] = None

            r = await client.put(f"/objects/products/{resolved.id}", json=body)
            r.raise_for_status()
            return CreateOk(created_object_id=resolved.id)
        except Exception as e:
            return CreateError(error=_format_exc(e))

    @mcp.tool()
    async def product_delete(
        product: Annotated[int | str, Field(description="Product to delete. Name or ID.")],
    ) -> CreateOk | CreateError:
        """Delete a product. Also removes every stock entry for the product — irreversible."""
        try:
            resolved = await resolver.resolve_product(product)
            r = await client.delete(f"/objects/products/{resolved.id}")
            r.raise_for_status()
            return CreateOk(created_object_id=resolved.id)
        except Exception as e:
            return CreateError(error=_format_exc(e))

    # ── Transfer stock ───────────────────────────────────────────────────

    @mcp.tool()
    async def stock_transfer(
        product: Annotated[int | str, Field(description=_PRODUCT_DESC)],
        amount: Annotated[float, Field(description="Amount to transfer, in `qu` units.")],
        qu: Annotated[int | str, Field(description=_QU_DESC)],
        from_location: Annotated[int | str, Field(description="Source location. Name or ID.")],
        to_location: Annotated[int | str, Field(description="Destination location. Name or ID.")],
    ) -> StockOpOk | StockOpError:
        """Move stock from one location to another (e.g. Cellar → Fridge).

        Stock totals don't change; only the per-location split does. The
        result's `amount_delta` and `new_amount` are null for transfers —
        Grocy doesn't return them. The `transaction_id` works with
        `transaction_undo` to revert. See also `stock_add`,
        `stock_consume`, `stock_set`.
        """
        try:
            prod = await resolver.resolve_product(product)
            from_loc = await resolver.resolve_location(from_location)
            to_loc = await resolver.resolve_location(to_location)
            rqu = await resolver.resolve_qu_for_product(qu, prod.id)

            stock_amount = amount * rqu.conversion_factor

            body: dict[str, Any] = {
                "amount": stock_amount,
                "location_id_from": from_loc.id,
                "location_id_to": to_loc.id,
            }
            r = await client.post(f"/stock/products/{prod.id}/transfer", json=body)
            r.raise_for_status()
            entries: list[dict[str, Any]] = r.json()
            tx_id = entries[0]["transaction_id"] if entries else None

            return StockOpOk(
                product_name=prod.name,
                transaction_id=tx_id,
                amount_delta=None,
                new_amount=None,
                qu_name=rqu.name,
                stock_qu_name=rqu.stock_qu_name if rqu.conversion_factor != 1.0 else None,
                location_name=f"{from_loc.name} -> {to_loc.name}",
            )
        except Exception as e:
            return StockOpError(error=_format_exc(e))

    # ── Shopping list ────────────────────────────────────────────────────

    @mcp.tool()
    async def shopping_list_get(
        shopping_list: Annotated[int | str, Field(description="Shopping list. Name or ID.")],
    ) -> dict[str, Any]:
        """Fetch a shopping list's metadata plus every item on it.

        Each returned item carries `item_id` (pass to
        `shopping_list_item_edit` or `shopping_list_items_remove`),
        `product_name` (null for note-only items), `amount`, `qu_name`,
        `note`, and `done`. Add items with `shopping_list_items_add`; empty
        the list with `shopping_list_clear`.
        """
        sl = await resolver.resolve_shopping_list(shopping_list)

        r = await client.get(f"/objects/shopping_lists/{sl.id}")
        r.raise_for_status()
        list_data: dict[str, Any] = r.json()

        r = await client.get("/objects/shopping_list")
        r.raise_for_status()
        all_items: list[dict[str, Any]] = r.json()
        items = [i for i in all_items if int(i.get("shopping_list_id", 1)) == sl.id]

        result_items = []
        for item in items:
            product_id = item.get("product_id")
            product_name = None
            qu_name = None
            if product_id is not None:
                product_name = await resolver.product_name(int(product_id))
                product_data = await resolver.get_product(int(product_id))
                if product_data:
                    qu_name = await resolver.qu_name(int(product_data["qu_id_stock"]))

            result_items.append(
                {
                    "item_id": int(item["id"]),
                    "product_name": product_name,
                    "product_id": int(product_id) if product_id is not None else None,
                    "amount": float(item.get("amount", 1)),
                    "qu_name": qu_name,
                    "note": item.get("note"),
                    "done": item.get("done") in (True, 1, "1"),
                }
            )

        return {
            "name": str(list_data.get("name", "")),
            "description": list_data.get("description"),
            "items": result_items,
        }

    @mcp.tool()
    async def shopping_list_items_add(items: list[ShoppingItem]) -> list[ShoppingListItemOk | ShoppingListItemError]:
        """Add items to a shopping list. Max 20 items per call.

        Each item needs a `shopping_list` (the target list, by name or ID).
        Items come in two shapes:

        - Product-linked: set `product` (name or ID) and `amount`.
        - Note-only: leave `product` null and set `note` — for free-form
          reminders like "check if we need paper towels".

        See `shopping_list_get` for the post-add view and
        `shopping_list_item_edit` / `shopping_list_items_remove` /
        `shopping_list_clear` for the other shopping-list operations.
        """
        _check_batch_size(items, "items")

        async def _one(item: ShoppingItem) -> ShoppingListItemOk | ShoppingListItemError:
            try:
                sl = await resolver.resolve_shopping_list(item.shopping_list)
                body: dict[str, Any] = {"shopping_list_id": sl.id, "amount": item.amount}
                product_name = None
                qu_name = None
                if item.product is not None:
                    prod = await resolver.resolve_product(item.product)
                    body["product_id"] = prod.id
                    product_name = prod.name
                    product_data = await resolver.get_product(prod.id)
                    if product_data:
                        qu_name = await resolver.qu_name(int(product_data["qu_id_stock"]))
                if item.note is not None:
                    body["note"] = item.note

                r = await client.post("/objects/shopping_list", json=body)
                r.raise_for_status()
                data = r.json()
                return ShoppingListItemOk(
                    item_id=int(data.get("created_object_id", 0)),
                    product_name=product_name,
                    amount=item.amount,
                    qu_name=qu_name,
                )
            except Exception as e:
                return ShoppingListItemError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def shopping_list_item_edit(
        item_id: Annotated[int, Field(description="Shopping-list item ID (from `shopping_list_get`).")],
        amount: Annotated[float | None, Field(description="New amount.")] = None,
        note: Annotated[str | None, Field(description="New free-text note.")] = None,
        done: Annotated[bool | None, Field(description="Mark the item done (checked) or not.")] = None,
        clear_fields: Annotated[
            set[EditShoppingListField] | None,
            Field(
                description=(
                    "Fields to explicitly null out. Only the values in `EditShoppingListField` are nullable "
                    "(just `note`); `amount` and `done` are NOT NULL. To change which product an item points "
                    "at, remove the item with `shopping_list_items_remove` and re-add it via "
                    "`shopping_list_items_add`."
                )
            ),
        ] = None,
    ) -> ShoppingListItemOk | ShoppingListItemError:
        """Partial update of one shopping-list item — only the fields you pass change.

        Updates `amount`, `note`, or `done` status. The linked product is
        immutable here; use `shopping_list_items_remove` + re-add via
        `shopping_list_items_add` to re-point.
        """
        try:
            r = await client.get(f"/objects/shopping_list/{item_id}")
            r.raise_for_status()
            current: dict[str, Any] = r.json()

            # Filter to writable columns — Grocy may return extra fields on GET.
            sl_writable = {"product_id", "amount", "note", "done", "shopping_list_id", "qu_id"}
            body = {k: v for k, v in current.items() if k in sl_writable}
            if amount is not None:
                body["amount"] = amount
            if note is not None:
                body["note"] = note
            if done is not None:
                body["done"] = int(done)
            if clear_fields:
                for field in clear_fields:
                    if field == EditShoppingListField.NOTE:
                        body["note"] = None

            r = await client.put(f"/objects/shopping_list/{item_id}", json=body)
            r.raise_for_status()

            product_id = body.get("product_id")
            product_name = None
            qu_name = None
            if product_id is not None:
                product_name = await resolver.product_name(int(product_id))
                product_data = await resolver.get_product(int(product_id))
                if product_data:
                    qu_name = await resolver.qu_name(int(product_data["qu_id_stock"]))

            return ShoppingListItemOk(
                item_id=item_id, product_name=product_name, amount=float(body.get("amount", 1)), qu_name=qu_name
            )
        except Exception as e:
            return ShoppingListItemError(error=_format_exc(e))

    @mcp.tool()
    async def shopping_list_items_remove(
        item_ids: Annotated[list[int], Field(description="Shopping-list item IDs (from `shopping_list_get`).")],
    ) -> list[ShoppingListItemOk | ShoppingListItemError]:
        """Remove specific items from a shopping list. Max 20 IDs per call.

        For removing every item on a list at once, use
        `shopping_list_clear` instead.
        """
        _check_batch_size(item_ids, "item_ids")

        async def _one(item_id: int) -> ShoppingListItemOk | ShoppingListItemError:
            try:
                r = await client.get(f"/objects/shopping_list/{item_id}")
                r.raise_for_status()
                item: dict[str, Any] = r.json()

                r = await client.delete(f"/objects/shopping_list/{item_id}")
                r.raise_for_status()

                product_id = item.get("product_id")
                product_name = None
                qu_name = None
                if product_id is not None:
                    product_name = await resolver.product_name(int(product_id))
                    product_data = await resolver.get_product(int(product_id))
                    if product_data:
                        qu_name = await resolver.qu_name(int(product_data["qu_id_stock"]))

                return ShoppingListItemOk(
                    item_id=item_id, product_name=product_name, amount=float(item.get("amount", 1)), qu_name=qu_name
                )
            except Exception as e:
                return ShoppingListItemError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(iid) for iid in item_ids]))

    @mcp.tool()
    async def shopping_list_clear(
        shopping_list: Annotated[int | str, Field(description="Shopping list to clear. Name or ID.")],
    ) -> dict[str, Any]:
        """Empty a shopping list — removes every item, keeps the list itself.

        Returns `{kind: "ok", cleared: N}` with the number removed. To
        remove specific items only, use `shopping_list_items_remove`.
        """
        sl = await resolver.resolve_shopping_list(shopping_list)

        r = await client.get("/objects/shopping_list")
        r.raise_for_status()
        all_items: list[dict[str, Any]] = r.json()
        items = [i for i in all_items if int(i.get("shopping_list_id", 1)) == sl.id]

        async def _delete(item_id: int) -> httpx.Response:
            return await client.delete(f"/objects/shopping_list/{item_id}")

        responses = await asyncio.gather(*[_retry(functools.partial(_delete, int(i["id"]))) for i in items])
        for resp in responses:
            resp.raise_for_status()

        return {"kind": "ok", "cleared": len(items)}

    # ── Query tools ──────────────────────────────────────────────────────

    @mcp.tool()
    async def get_expiring_stock(
        days_ahead: Annotated[int, Field(description="Window to look ahead, in days.")],
    ) -> list[dict[str, Any]]:
        """Stock due to expire within the next `days_ahead` days.

        Each row carries product name, amount, unit, location,
        best-before date, and a pre-computed `days_until_expiry`. For
        already-expired stock use `get_expired_stock`; for low-stock use
        `get_below_minimum_stock`.
        """
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

        # Prefetch reference tables once — the resolver is intentionally
        # non-caching across tool calls, but within one call the state can't
        # mutate so local id->name maps are safe and save O(rows * 2) fetches.
        qu_names = {int(q["id"]): str(q["name"]) for q in await resolver.all_qus()}
        location_names = {int(row["id"]): str(row["name"]) for row in await resolver.all_locations()}

        cutoff = datetime.now(tz=UTC).date() + timedelta(days=days_ahead)
        result = []

        for entry in data.get("due_products", []):
            product = entry.get("product") or {}
            bbd = entry.get("best_before_date")
            if not bbd:
                continue
            try:
                expiry_date = datetime.strptime(bbd, "%Y-%m-%d").date()
            except ValueError:
                continue
            if expiry_date > cutoff:
                continue

            days_until = (expiry_date - datetime.now(tz=UTC).date()).days
            qu_id = product.get("qu_id_stock")
            qu_name = qu_names.get(int(qu_id), "unknown") if qu_id is not None else "unknown"
            loc_id = product.get("location_id")
            location_name = location_names.get(int(loc_id), "unknown") if loc_id is not None else "unknown"

            result.append(
                {
                    "product_id": int(entry["product_id"]),
                    "product_name": str(product.get("name", "")),
                    "amount": float(entry.get("amount", 0)),
                    "qu_name": qu_name,
                    "location_name": location_name,
                    "best_before_date": bbd,
                    "days_until_expiry": days_until,
                }
            )
        return result

    @mcp.tool()
    async def get_below_minimum_stock() -> list[dict[str, Any]]:
        """Products under their `min_stock_amount` threshold.

        Each row gives product name, current amount, minimum amount,
        unit, and `deficit` (how much is missing). Products with
        `min_stock_amount = 0` never appear here — set the threshold
        via `products_create` / `product_edit`. See also
        `get_expiring_stock` and `get_expired_stock`.
        """
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

        qu_names = {int(q["id"]): str(q["name"]) for q in await resolver.all_qus()}

        result = []
        for entry in data.get("missing_products", []):
            product = entry.get("product") or {}
            qu_id = product.get("qu_id_stock")
            qu_name = qu_names.get(int(qu_id), "unknown") if qu_id is not None else "unknown"

            amount_missing = float(entry.get("amount_missing", 0))
            min_amount = float(product.get("min_stock_amount", 0))
            current = min_amount - amount_missing if amount_missing > 0 else 0

            result.append(
                {
                    "product_id": int(entry.get("id", 0)),
                    "product_name": str(product.get("name", "")),
                    "amount": current,
                    "min_amount": min_amount,
                    "qu_name": qu_name,
                    "deficit": amount_missing,
                }
            )
        return result

    @mcp.tool()
    async def get_expired_stock() -> list[dict[str, Any]]:
        """Stock that's already past its best-before date.

        Each row carries product name, amount, unit, location,
        best-before date, and `days_overdue`. For things due to expire
        but not yet expired, use `get_expiring_stock`. For low-stock,
        use `get_below_minimum_stock`.
        """

        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

        qu_names = {int(q["id"]): str(q["name"]) for q in await resolver.all_qus()}
        location_names = {int(row["id"]): str(row["name"]) for row in await resolver.all_locations()}

        result = []
        for entry in data.get("expired_products", []):
            product = entry.get("product") or {}
            bbd = entry.get("best_before_date")
            qu_id = product.get("qu_id_stock")
            qu_name = qu_names.get(int(qu_id), "unknown") if qu_id is not None else "unknown"
            loc_id = product.get("location_id")
            location_name = location_names.get(int(loc_id), "unknown") if loc_id is not None else "unknown"

            days_overdue = 0
            if bbd:
                try:
                    expiry_date = datetime.strptime(bbd, "%Y-%m-%d").date()
                    days_overdue = (datetime.now(tz=UTC).date() - expiry_date).days
                except ValueError:
                    pass

            result.append(
                {
                    "product_id": int(entry.get("product_id", 0)),
                    "product_name": str(product.get("name", "")),
                    "amount": float(entry.get("amount", 0)),
                    "qu_name": qu_name,
                    "location_name": location_name,
                    "best_before_date": bbd,
                    "days_overdue": days_overdue,
                }
            )
        return result
