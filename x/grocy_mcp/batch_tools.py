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
from pydantic import BaseModel, Field

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
    """Format exception with full traceback for error reporting."""
    return "".join(traceback.format_exception(e))


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


class EntityType(StrEnum):
    """Grocy entity type names exposed through the /objects/{entity} API."""

    PRODUCTS = "products"
    CHORES = "chores"
    PRODUCT_BARCODES = "product_barcodes"
    BATTERIES = "batteries"
    LOCATIONS = "locations"
    QUANTITY_UNITS = "quantity_units"
    QUANTITY_UNIT_CONVERSIONS = "quantity_unit_conversions"
    SHOPPING_LIST = "shopping_list"
    SHOPPING_LISTS = "shopping_lists"
    SHOPPING_LOCATIONS = "shopping_locations"
    RECIPES = "recipes"
    RECIPES_POS = "recipes_pos"
    RECIPES_NESTINGS = "recipes_nestings"
    TASKS = "tasks"
    TASK_CATEGORIES = "task_categories"
    PRODUCT_GROUPS = "product_groups"
    EQUIPMENT = "equipment"
    API_KEYS = "api_keys"
    USERFIELDS = "userfields"
    USERENTITIES = "userentities"
    USEROBJECTS = "userobjects"
    MEAL_PLAN = "meal_plan"
    STOCK_LOG = "stock_log"
    STOCK = "stock"
    STOCK_CURRENT_LOCATIONS = "stock_current_locations"
    CHORES_LOG = "chores_log"
    MEAL_PLAN_SECTIONS = "meal_plan_sections"
    PRODUCTS_LAST_PURCHASED = "products_last_purchased"
    PRODUCTS_AVERAGE_PRICE = "products_average_price"
    QUANTITY_UNIT_CONVERSIONS_RESOLVED = "quantity_unit_conversions_resolved"
    RECIPES_POS_RESOLVED = "recipes_pos_resolved"
    BATTERY_CHARGE_CYCLES = "battery_charge_cycles"
    PRODUCT_BARCODES_VIEW = "product_barcodes_view"
    PERMISSION_HIERARCHY = "permission_hierarchy"


# ── Shared input/output types ────────────────────────────────────────────────


class CreateItem(BaseModel):
    entity_type: EntityType
    body: dict[str, Any]


class CreateOk(BaseModel):
    kind: Literal["ok"] = "ok"
    created_object_id: int | None = None


class CreateError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class GetOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entity_type: EntityType
    object_id: int
    data: dict[str, Any]


class GetError(BaseModel):
    kind: Literal["error"] = "error"
    entity_type: EntityType
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


class AddItem(BaseModel):
    product: int | str = Field(description="Product ID (int) or name (str).")
    amount: float
    qu: int | str = Field(
        description="Quantity unit ID or name. Must match the product's stock QU, or have a valid conversion."
    )
    location: int | str = Field(description="Storage location ID or name. Required — no default.")
    best_before_date: date | None = Field(default=None, description="ISO date (YYYY-MM-DD). Omit for no expiry.")
    price: float | None = Field(default=None, description="Omit to use last recorded price.")
    note: str | None = None


class ConsumeItem(BaseModel):
    product: int | str = Field(description="Product ID (int) or name (str).")
    amount: float
    qu: int | str = Field(
        description="Quantity unit ID or name. Must match the product's stock QU, or have a valid conversion."
    )
    location: int | str = Field(description="Location to consume from. Required — no default.")
    spoiled: bool = False
    allow_subproduct_substitution: bool = False


class InventoryItem(BaseModel):
    product: int | str = Field(description="Product ID (int) or name (str).")
    new_amount: float = Field(
        description="Absolute target stock amount in stock QU. Grocy adds or removes to reach it."
    )
    qu: int | str = Field(
        description="Quantity unit ID or name. Must match the product's stock QU, or have a valid conversion."
    )
    location: int | str = Field(description="Location for added units. Required — no default.")
    best_before_date: date | None = Field(default=None, description="For added units (YYYY-MM-DD). Omit for no expiry.")
    price: float | None = Field(default=None, description="For added units. Omit to use last recorded price.")


class StockOpOk(BaseModel):
    kind: Literal["ok"] = "ok"
    product_name: str
    transaction_id: str | None = Field(default=None, description="Grocy transaction ID for undo.")
    amount_delta: float | None = Field(default=None, description="Net stock change (negative for consume).")
    new_amount: float | None = Field(
        default=None, description="Best-effort resulting stock amount. May be None if the follow-up read fails."
    )
    qu_name: str = Field(description="Name of the quantity unit for the amounts.")
    stock_qu_name: str | None = Field(
        default=None, description="Stock QU name, if different from qu_name (QU conversion was applied)."
    )
    location_name: str


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
    """Fields that can be explicitly cleared (set to null) on a stock entry."""

    PRICE = "price"
    BEST_BEFORE_DATE = "best_before_date"
    PURCHASED_DATE = "purchased_date"
    NOTE = "note"


# ── Product management types ──────────────────────────────────────────────────


class EditProductField(StrEnum):
    """Fields that can be explicitly cleared (set to null) on a product."""

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
    product: int | str | None = Field(default=None, description="Product ID or name. None for note-only items.")
    amount: float = 1
    note: str | None = None
    shopping_list: int | str = Field(description="Shopping list ID or name.")


class ShoppingListItemResult(BaseModel):
    kind: Literal["ok"] = "ok"
    item_id: int
    product_name: str | None = None
    amount: float
    qu_name: str | None = None


class ShoppingListItemError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class EditShoppingListField(StrEnum):
    NOTE = "note"


# ── Tool registration ────────────────────────────────────────────────────────


def register_batch_tools(mcp: FastMCP, client: httpx.AsyncClient, settings: ServerSettings) -> None:
    """Register custom batch tools on an existing FastMCP instance."""
    sem = asyncio.Semaphore(settings.max_concurrent_requests)
    max_batch = settings.max_batch_size
    max_retries = settings.max_retries
    base_delay = settings.retry_base_delay

    def _check_batch_size(items: list[Any] | set[Any], label: str) -> None:
        if len(items) > max_batch:
            raise ValueError(f"batch too large: {len(items)} {label} exceeds maximum of {max_batch}")

    async def _retry[T](fn: Callable[[], Awaitable[T]]) -> T:
        return await _with_retry(fn, sem, max_retries=max_retries, base_delay=base_delay)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _enrich_stock_entry(resolver: EntityResolver, entry_data: dict[str, Any]) -> StockEntryDetail:
        """Convert a raw Grocy stock entry dict to a StockEntryDetail with names."""
        product_id = int(entry_data["product_id"])
        product_name = await resolver.product_name(product_id)
        product = await resolver.get_product(product_id)
        stock_qu_id = int(product["qu_id_stock"]) if product else 0
        qu_name = await resolver.qu_name(stock_qu_id)
        loc_id = entry_data.get("location_id")
        if loc_id is not None:
            location_name = await resolver.location_name(int(loc_id))
        else:
            # Fall back to product's default location
            default_loc = int(product["location_id"]) if product else 0
            location_name = await resolver.location_name(default_loc)
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
    async def create_entities(items: list[CreateItem]) -> list[CreateOk | CreateError]:
        """Create multiple entities (locations, quantity_units, etc.) in one call. Max 20.

        Use this for entity types that don't have a dedicated tool (e.g., use
        create_product for products instead). Each item needs entity_type and a
        body dict with that entity's fields. Failed items return errors without
        aborting others. Returns created_object_id per item on success.
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
    async def list_entities(entity_types: list[EntityType]) -> dict[str, list[Any]]:
        """Fetch all objects for multiple entity types in one call. Max 20 types.

        Use list_products, list_locations, or list_quantity_units for those types
        (they support brief/full modes). Use this tool for less common types like
        product_barcodes, shopping_lists, quantity_unit_conversions, etc.
        Returns {entity_type: [objects]}.
        """
        _check_batch_size(entity_types, "entity_types")

        async def _fetch(entity_type: EntityType) -> tuple[str, list[Any]]:
            async def _do() -> tuple[str, list[Any]]:
                r = await client.get(f"/objects/{entity_type}")
                r.raise_for_status()
                return str(entity_type), r.json()

            return await _retry(_do)

        pairs = await asyncio.gather(*[_fetch(et) for et in entity_types])
        return dict(pairs)

    @mcp.tool()
    async def get_entities(entity_type: EntityType, object_ids: list[int]) -> list[GetOk | GetError]:
        """Fetch specific objects by ID for one entity type. Max 20 IDs.

        Use this when you already know the IDs (e.g., from a previous list or
        create call). Each result carries entity_type and object_id for
        identification. Failed fetches return errors without aborting others.
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
    async def get_stock(
        products: Annotated[
            list[int | str] | None, Field(description="Filter to these products (ID or name). None = all.")
        ] = None,
        locations: Annotated[
            list[int | str] | None, Field(description="Filter to these locations (ID or name). None = all.")
        ] = None,
    ) -> list[StockEntry]:
        """Get current stock overview. Returns a compact list with product name, amount,
        quantity unit, location, and best-before date for each stocked product.

        Use this to answer "what's in stock?" or check specific products/locations.
        Pass product names or IDs in 'products' and/or location names or IDs in
        'locations' to filter. Filters are AND-ed. Omit both for the full inventory.
        """
        resolver = EntityResolver(client)

        r = await _retry(lambda: client.get("/stock"))
        r.raise_for_status()
        stock_data: list[dict[str, Any]] = r.json()

        # Resolve filters to IDs
        product_ids: set[int] | None = None
        if products is not None:
            product_ids = set()
            for ref in products:
                resolved = await resolver.resolve_product(ref)
                product_ids.add(resolved.id)

        location_ids: set[int] | None = None
        if locations is not None:
            location_ids = set()
            for ref in locations:
                resolved = await resolver.resolve_location(ref)
                location_ids.add(resolved.id)

        result = []
        for entry in stock_data:
            pid = int(entry["product_id"])
            if product_ids is not None and pid not in product_ids:
                continue

            product = entry.get("product") or {}
            loc_id_raw = product.get("location_id")
            loc_id = int(loc_id_raw) if loc_id_raw is not None else None

            if location_ids is not None and (loc_id is None or loc_id not in location_ids):
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
    async def add_stock(items: list[AddItem]) -> list[StockOpOk | StockOpError]:
        """Add stock for one or more products. Max 20 items per call.

        Each item needs: product (name or ID), amount, qu (quantity unit name
        or ID), and location (name or ID). All four are required — no defaults.

        The qu must match the product's stock QU or have a valid conversion
        (e.g., "Crate" converts to "Bottle" automatically if a conversion exists).

        Returns transaction_id (for undo via undo_transaction), the amount
        added, and new_amount (best-effort — may be None if the follow-up
        read fails).
        """
        _check_batch_size(items, "items")
        resolver = EntityResolver(client)

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
    async def consume_stock(items: list[ConsumeItem]) -> list[StockOpOk | StockOpError]:
        """Consume (use up) stock for one or more products. Max 20 items per call.

        Each item needs: product, amount, qu, and location — all required.
        Location is mandatory to prevent consuming from the wrong place
        (important when one Grocy instance manages multiple households).

        If you don't know where the product is stored, use get_stock first.
        Returns transaction_id for undo and new_amount (best-effort).
        """
        _check_batch_size(items, "items")
        resolver = EntityResolver(client)

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
    async def inventory_stock(items: list[InventoryItem]) -> list[StockOpOk | StockOpError]:
        """Set absolute stock amounts for one or more products. Max 20 items.

        Use this for corrections: "we actually have 10 kg of rice, not 3."
        Grocy computes the delta and adds or removes stock to reach new_amount.
        best_before_date, price apply only to units being added (if any).
        Location is required — no silent default.
        """
        _check_batch_size(items, "items")
        resolver = EntityResolver(client)

        async def _one(item: InventoryItem) -> StockOpOk | StockOpError:
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
    async def get_stock_entries(
        products: Annotated[
            list[int | str] | None,
            Field(description="Product names or IDs. Returns all stock entries for these products."),
        ] = None,
        entry_ids: Annotated[list[int] | None, Field(description="Specific stock entry IDs to fetch.")] = None,
    ) -> list[StockEntryOk | StockEntryError]:
        """Get individual stock entries — the line items that make up a product's stock.

        Two modes (provide exactly one):
        - products: get all entries for one or more products (by name or ID).
          Use this to see what's in stock per entry, with dates, prices, and locations.
        - entry_ids: get specific entries by ID (e.g., for use with edit_stock_entry).

        Each entry includes product_name, amount, qu_name, location_name, dates, price, open status.
        """
        if (products is None) == (entry_ids is None):
            raise ValueError("Provide exactly one of 'products' or 'entry_ids', not both or neither.")

        resolver = EntityResolver(client)

        if products is not None:
            _check_batch_size(products, "products")
            all_results: list[StockEntryOk | StockEntryError] = []
            for product_ref in products:
                resolved = await resolver.resolve_product(product_ref)
                try:
                    r = await _retry(functools.partial(client.get, f"/stock/products/{resolved.id}/entries"))
                    r.raise_for_status()
                    entries_data: list[dict[str, Any]] = r.json()
                    for entry_data in entries_data:
                        try:
                            detail = await _enrich_stock_entry(resolver, entry_data)
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
                    detail = await _enrich_stock_entry(resolver, r.json())
                    return StockEntryOk(entry=detail)

                return await _retry(_do)
            except Exception as e:
                return StockEntryError(entry_id=entry_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_one(eid) for eid in entry_ids]))

    @mcp.tool()
    async def edit_stock_entry(
        entry_id: Annotated[int, Field(description="ID of the stock entry to edit.")],
        amount: Annotated[float | None, Field(description="New amount.")] = None,
        best_before_date: Annotated[date | None, Field(description="New best-before date (YYYY-MM-DD).")] = None,
        purchased_date: Annotated[date | None, Field(description="New purchased date (YYYY-MM-DD).")] = None,
        price: Annotated[float | None, Field(description="New price.")] = None,
        location: Annotated[int | str | None, Field(description="New location (ID or name).")] = None,
        open: Annotated[bool | None, Field(description="Whether the entry is opened.")] = None,
        note: Annotated[str | None, Field(description="New note.")] = None,
        clear_fields: Annotated[
            set[EditStockEntryField] | None,
            Field(description="Fields to explicitly set to null (e.g., ['price'] to remove a price)."),
        ] = None,
    ) -> StockEntryOk | StockEntryError:
        """Edit a stock entry (partial update). Only the fields you specify are changed.

        Pass new values for fields you want to change (e.g., price=9.99).
        To clear a nullable field to "no value", include it in clear_fields
        (e.g., clear_fields=["price"]). Omitted fields stay as they are.

        The server reads the current entry, merges your changes, and writes
        back — you never need to copy-paste fields you're not changing.
        """
        resolver = EntityResolver(client)

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
            detail = await _enrich_stock_entry(resolver, updated_r.json())
            return StockEntryOk(entry=detail, changes=changes or None)
        except Exception as e:
            return StockEntryError(entry_id=entry_id, error=_format_exc(e))

    # ── Reference data ───────────────────────────────────────────────────

    @mcp.tool()
    async def list_products(
        detail: Annotated[
            Literal["brief", "full"],
            Field(description="'brief' returns id + name only. 'full' returns all Grocy fields."),
        ] = "brief",
    ) -> list[dict[str, Any]]:
        """List all products. Returns id + name by default ('brief'). Use 'full' for all Grocy fields.

        Use this to find product names/IDs before calling stock operations.
        Most other tools accept product names directly, so you may not need this.
        """
        resolver = EntityResolver(client)
        rows = await resolver.all_products()
        if detail == "brief":
            return [{"id": int(r["id"]), "name": str(r["name"])} for r in rows]
        return rows

    @mcp.tool()
    async def list_locations(
        detail: Annotated[
            Literal["brief", "full"], Field(description="'brief' returns id + name. 'full' returns all fields.")
        ] = "brief",
    ) -> list[dict[str, Any]]:
        """List all storage locations (e.g., Fridge, Pantry, Freezer).

        Use this to find location names/IDs before adding or consuming stock.
        Stock operations accept location names directly.
        """
        resolver = EntityResolver(client)
        rows = await resolver.all_locations()
        if detail == "brief":
            return [{"id": int(r["id"]), "name": str(r["name"])} for r in rows]
        return rows

    @mcp.tool()
    async def list_quantity_units(
        detail: Annotated[
            Literal["brief", "full"],
            Field(description="'brief' returns id + name + name_plural. 'full' returns all fields."),
        ] = "brief",
    ) -> list[dict[str, Any]]:
        """List all quantity units (e.g., Kilogram, Piece, Liter).

        Stock operations require specifying the quantity unit. Use this to
        discover available units. Most tools accept unit names directly.
        """
        resolver = EntityResolver(client)
        rows = await resolver.all_qus()
        if detail == "brief":
            return [{"id": int(r["id"]), "name": str(r["name"]), "name_plural": r.get("name_plural")} for r in rows]
        return rows

    @mcp.tool()
    async def list_product_groups(
        detail: Annotated[
            Literal["brief", "full"], Field(description="'brief' returns id + name. 'full' returns all fields.")
        ] = "brief",
    ) -> list[dict[str, Any]]:
        """List all product groups (categories for organizing products).

        Product groups are optional — products can exist without one.
        Use group names or IDs when creating or editing products.
        """
        resolver = EntityResolver(client)
        rows = await resolver.all_product_groups()
        if detail == "brief":
            return [{"id": int(r["id"]), "name": str(r["name"])} for r in rows]
        return rows

    # ── Product management ───────────────────────────────────────────────

    @mcp.tool()
    async def create_product(
        name: Annotated[str, Field(description="Product name. Must be unique.")],
        stock_qu: Annotated[int | str, Field(description="Stock quantity unit (ID or name).")],
        location: Annotated[int | str, Field(description="Default storage location (ID or name).")],
        purchase_qu: Annotated[int | str | None, Field(description="Purchase QU. Defaults to stock_qu.")] = None,
        min_stock_amount: Annotated[float, Field(description="Minimum stock amount for low-stock alerts.")] = 0,
        default_best_before_days: Annotated[
            int, Field(description="Auto-calculated expiry days on add. 0 = no auto-expiry.")
        ] = 0,
        product_group: Annotated[int | str | None, Field(description="Product group/category (ID or name).")] = None,
        description: Annotated[str | None, Field(description="Product description.")] = None,
    ) -> CreateOk | CreateError:
        """Create a new product. Requires name, stock quantity unit, and default location.

        All references (stock_qu, location, purchase_qu, product_group) accept
        names or IDs. purchase_qu defaults to stock_qu if omitted.
        Use list_quantity_units and list_locations to discover available values.
        """
        resolver = EntityResolver(client)
        try:
            loc = await resolver.resolve_location(location)
            squ = await resolver.resolve_qu(stock_qu)
            pqu = await resolver.resolve_qu(purchase_qu) if purchase_qu is not None else squ

            body: dict[str, Any] = {
                "name": name,
                "location_id": loc.id,
                "qu_id_stock": squ.id,
                "qu_id_purchase": pqu.id,
                "min_stock_amount": min_stock_amount,
                "default_best_before_days": default_best_before_days,
            }
            if product_group is not None:
                pg = await resolver.resolve_product_group(product_group)
                body["product_group_id"] = pg.id
            if description is not None:
                body["description"] = description

            r = await client.post("/objects/products", json=body)
            r.raise_for_status()
            data = r.json()
            raw_id = data.get("created_object_id")
            return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)
        except Exception as e:
            return CreateError(error=_format_exc(e))

    @mcp.tool()
    async def edit_product(
        product: Annotated[int | str, Field(description="Product to edit (ID or name).")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        stock_qu: Annotated[int | str | None, Field(description="New stock QU (ID or name).")] = None,
        location: Annotated[int | str | None, Field(description="New default location (ID or name).")] = None,
        purchase_qu: Annotated[int | str | None, Field(description="New purchase QU (ID or name).")] = None,
        min_stock_amount: Annotated[float | None, Field(description="New minimum stock amount.")] = None,
        default_best_before_days: Annotated[int | None, Field(description="New auto-expiry days.")] = None,
        product_group: Annotated[int | str | None, Field(description="New product group (ID or name).")] = None,
        description: Annotated[str | None, Field(description="New description.")] = None,
        clear_fields: Annotated[set[EditProductField] | None, Field(description="Fields to set to null.")] = None,
    ) -> CreateOk | CreateError:
        """Edit a product (partial update). Only the fields you specify are changed.

        To clear a nullable field (description, product_group, etc.), include it
        in clear_fields. Omitted fields stay as they are. The server reads the
        current product, merges your changes, and writes back.
        """
        resolver = EntityResolver(client)
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
    async def delete_product(
        product: Annotated[int | str, Field(description="Product to delete (ID or name).")],
    ) -> CreateOk | CreateError:
        """Delete a product by name or ID. This also removes all stock entries for the product."""
        resolver = EntityResolver(client)
        try:
            resolved = await resolver.resolve_product(product)
            r = await client.delete(f"/objects/products/{resolved.id}")
            r.raise_for_status()
            return CreateOk(created_object_id=resolved.id)
        except Exception as e:
            return CreateError(error=_format_exc(e))

    # ── Transfer stock ───────────────────────────────────────────────────

    @mcp.tool()
    async def transfer_stock(
        product: Annotated[int | str, Field(description="Product to transfer (ID or name).")],
        amount: Annotated[float, Field(description="Amount to transfer.")],
        qu: Annotated[
            int | str, Field(description="Quantity unit (ID or name). Must match stock QU or have conversion.")
        ],
        from_location: Annotated[int | str, Field(description="Source location (ID or name).")],
        to_location: Annotated[int | str, Field(description="Destination location (ID or name).")],
    ) -> StockOpOk | StockOpError:
        """Move stock from one location to another (e.g., Cellar to Fridge).

        Both from_location and to_location are required. QU must match the
        product's stock QU or have a valid conversion. Returns transaction_id for undo.
        """
        resolver = EntityResolver(client)
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
    async def get_shopping_list(
        shopping_list: Annotated[int | str, Field(description="Shopping list ID or name.")],
    ) -> dict[str, Any]:
        """Get all items on a shopping list, identified by name or ID.

        Returns the list name, description, and items. Each item has: product_name
        (null for note-only items), amount, qu_name, note, and done (checkbox) status.
        Item IDs are included for use with edit_shopping_list_item and remove_from_shopping_list.
        """
        resolver = EntityResolver(client)
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
    async def add_to_shopping_list(items: list[ShoppingItem]) -> list[ShoppingListItemResult | ShoppingListItemError]:
        """Add items to a shopping list. Max 20 items per call.

        Each item needs a shopping_list (name or ID). Items can be:
        - Product-linked: set product (name or ID) and amount.
        - Note-only: set note, leave product null. Good for reminders like
          "check if we need paper towels".
        """
        _check_batch_size(items, "items")
        resolver = EntityResolver(client)

        async def _one(item: ShoppingItem) -> ShoppingListItemResult | ShoppingListItemError:
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
                return ShoppingListItemResult(
                    item_id=int(data.get("created_object_id", 0)),
                    product_name=product_name,
                    amount=item.amount,
                    qu_name=qu_name,
                )
            except Exception as e:
                return ShoppingListItemError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def edit_shopping_list_item(
        item_id: Annotated[int, Field(description="Shopping list item ID.")],
        amount: Annotated[float | None, Field(description="New amount.")] = None,
        note: Annotated[str | None, Field(description="New note.")] = None,
        done: Annotated[bool | None, Field(description="Mark as done/not done.")] = None,
        clear_fields: Annotated[set[EditShoppingListField] | None, Field(description="Fields to set to null.")] = None,
    ) -> ShoppingListItemResult | ShoppingListItemError:
        """Edit a shopping list item (partial update). Change amount, note, or done status.

        Cannot change the product — delete and re-add instead.
        To clear the note, use clear_fields=["note"].
        """
        resolver = EntityResolver(client)
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

            return ShoppingListItemResult(
                item_id=item_id, product_name=product_name, amount=float(body.get("amount", 1)), qu_name=qu_name
            )
        except Exception as e:
            return ShoppingListItemError(error=_format_exc(e))

    @mcp.tool()
    async def remove_from_shopping_list(
        item_ids: Annotated[list[int], Field(description="Shopping list item IDs to remove.")],
    ) -> list[ShoppingListItemResult | ShoppingListItemError]:
        """Remove items from a shopping list by item ID (from get_shopping_list). Max 20."""
        _check_batch_size(item_ids, "item_ids")
        resolver = EntityResolver(client)

        async def _one(item_id: int) -> ShoppingListItemResult | ShoppingListItemError:
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

                return ShoppingListItemResult(
                    item_id=item_id, product_name=product_name, amount=float(item.get("amount", 1)), qu_name=qu_name
                )
            except Exception as e:
                return ShoppingListItemError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(iid) for iid in item_ids]))

    @mcp.tool()
    async def clear_shopping_list(
        shopping_list: Annotated[int | str, Field(description="Shopping list to clear (ID or name).")],
    ) -> dict[str, Any]:
        """Remove all items from a shopping list. The list itself is kept (just emptied)."""
        resolver = EntityResolver(client)
        sl = await resolver.resolve_shopping_list(shopping_list)

        r = await client.get("/objects/shopping_list")
        r.raise_for_status()
        all_items: list[dict[str, Any]] = r.json()
        items = [i for i in all_items if int(i.get("shopping_list_id", 1)) == sl.id]

        for item in items:
            await client.delete(f"/objects/shopping_list/{item['id']}")

        return {"kind": "ok", "cleared": len(items)}

    # ── Query tools ──────────────────────────────────────────────────────

    @mcp.tool()
    async def get_expiring_stock(
        days_ahead: Annotated[int, Field(description="Number of days to look ahead.")] = 7,
    ) -> list[dict[str, Any]]:
        """Get products expiring soon (within days_ahead days, default 7).

        Use this to answer "what's about to expire?" Returns product name, amount,
        unit, location, best-before date, and pre-computed days_until_expiry.
        """
        resolver = EntityResolver(client)
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

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
            qu_name = await resolver.qu_name(int(qu_id)) if qu_id is not None else "unknown"
            loc_id = product.get("location_id")
            location_name = await resolver.location_name(int(loc_id)) if loc_id is not None else "unknown"

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
        """Get products below their minimum stock level.

        Use this to answer "what are we running low on?" Returns product name,
        current amount, minimum amount, unit, and deficit (how much is missing).
        Products with min_stock_amount=0 never appear here.
        """
        resolver = EntityResolver(client)
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

        result = []
        for entry in data.get("missing_products", []):
            product = entry.get("product") or {}
            qu_id = product.get("qu_id_stock")
            qu_name = await resolver.qu_name(int(qu_id)) if qu_id is not None else "unknown"

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
        """Get products that have already passed their best-before date.

        Returns product name, amount, unit, location, best-before date, and
        days_overdue. Use this to find items that should be consumed soon or discarded.
        """

        resolver = EntityResolver(client)
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()

        result = []
        for entry in data.get("expired_products", []):
            product = entry.get("product") or {}
            bbd = entry.get("best_before_date")
            qu_id = product.get("qu_id_stock")
            qu_name = await resolver.qu_name(int(qu_id)) if qu_id is not None else "unknown"
            loc_id = product.get("location_id")
            location_name = await resolver.location_name(int(loc_id)) if loc_id is not None else "unknown"

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
