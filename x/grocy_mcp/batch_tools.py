"""Batch operation tools for Grocy MCP server.

Custom tools that replace several single-shot OpenAPI-generated tools with
batch/enriched versions. Each operation fans out concurrently via asyncio.gather
and collects results (continue-and-collect, never fail-fast).
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field


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


# ── Shared input/output types ──────────────────────────────────────────────


class CreateItem(BaseModel):
    entity_type: EntityType
    # TODO: discriminated union per entity_type for body (avoids having to know per-entity field names)
    body: dict[str, Any]


class CreateOk(BaseModel):
    kind: Literal["ok"] = "ok"
    index: int
    created_object_id: int | None = None


class CreateError(BaseModel):
    kind: Literal["error"] = "error"
    index: int
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


class StockEnrichedEntry(BaseModel):
    product_id: int
    amount: float
    amount_aggregated: float
    amount_opened: float
    best_before_date: str | None = None
    is_aggregated_amount: bool
    product: dict[str, Any]
    quantity_unit: dict[str, Any] | None = None
    location: dict[str, Any] | None = None


class AddItem(BaseModel):
    product_id: int
    amount: float
    best_before_date: str | None = Field(default=None, description="ISO date. Omit → today.")
    price: float | None = Field(default=None, description="Omit → last recorded price for product.")
    location_id: int | None = Field(default=None, description="Omit → product's default location.")
    note: str | None = None


class ConsumeItem(BaseModel):
    product_id: int
    amount: float
    spoiled: bool = False
    location_id: int | None = Field(
        default=None,
        description=(
            "Which location to consume from. Omit → consume from any location (Grocy picks). "
            # TODO: consider requiring explicit location_id when the product has stock in multiple locations,
            # to avoid silently consuming from the wrong place. For now, rely on callers to check.
            "If the product has stock in multiple locations, specify location_id to be explicit."
        ),
    )
    allow_subproduct_substitution: bool = False


class InventoryItem(BaseModel):
    product_id: int
    new_amount: float = Field(description="Absolute target stock amount. Grocy adds or removes to reach it.")
    best_before_date: str | None = Field(
        default=None, description="Applies only to units being added. Omit → no due date for added units."
    )
    location_id: int | None = Field(
        default=None, description="Applies only to units being added. Omit → product's default location."
    )
    price: float | None = Field(
        default=None, description="Applies only to units being added. Omit → last recorded price."
    )


class StockOpResult(BaseModel):
    index: int
    ok: bool
    transaction_id: str | None = Field(default=None, description="Grocy transaction ID for per-operation undo.")
    amount_delta: float | None = Field(
        default=None, description="Net stock change applied (negative for consume/removal)."
    )
    new_amount: float | None = Field(default=None, description="Resulting stock amount after the operation.")
    error: str | None = None


# ── Tool registration ──────────────────────────────────────────────────────


def register_batch_tools(mcp: FastMCP, client: httpx.AsyncClient) -> None:
    """Register custom batch tools on an existing FastMCP instance."""

    @mcp.tool()
    async def create_entities(items: list[CreateItem]) -> list[CreateOk | CreateError]:
        """Create multiple Grocy entities in one call.

        Sends one POST /objects/{entity_type} per item concurrently. Failed items
        are collected as CreateError; they do not abort others.
        """

        async def _one(i: int, item: CreateItem) -> CreateOk | CreateError:
            try:
                r = await client.post(f"/objects/{item.entity_type}", json=item.body)
                r.raise_for_status()
                data = r.json()
                raw_id = data.get("created_object_id")
                return CreateOk(index=i, created_object_id=int(raw_id) if raw_id is not None else None)
            except Exception as e:
                return CreateError(index=i, error=str(e))

        return list(await asyncio.gather(*[_one(i, item) for i, item in enumerate(items)]))

    @mcp.tool()
    async def list_entities(entity_types: list[EntityType]) -> dict[str, list[Any]]:
        """Fetch multiple Grocy entity types in one call.

        Returns a mapping of entity_type → list of entity objects, fetched
        concurrently. Raises on the first failed fetch (fail-fast). Use
        separate calls if partial failure tolerance is needed.
        """

        async def _fetch(entity_type: EntityType) -> tuple[str, list[Any]]:
            r = await client.get(f"/objects/{entity_type}")
            r.raise_for_status()
            return str(entity_type), r.json()

        pairs = await asyncio.gather(*[_fetch(et) for et in entity_types])
        return dict(pairs)

    @mcp.tool()
    async def get_entities(entity_type: EntityType, object_ids: list[int]) -> list[GetOk | GetError]:
        """Fetch multiple Grocy objects of the same entity type by ID.

        Returns one GetResult per ID, each carrying entity_type and object_id for
        unambiguous identification without index-matching. Failed fetches are
        returned as GetError and do not abort others.
        """

        async def _one(object_id: int) -> GetOk | GetError:
            try:
                r = await client.get(f"/objects/{entity_type}/{object_id}")
                r.raise_for_status()
                return GetOk(entity_type=entity_type, object_id=object_id, data=r.json())
            except Exception as e:
                return GetError(entity_type=entity_type, object_id=object_id, error=str(e))

        return list(await asyncio.gather(*[_one(oid) for oid in object_ids]))

    @mcp.tool()
    async def get_stock(
        include_quantity_unit: bool = False, include_location: bool = False
    ) -> list[StockEnrichedEntry]:
        """Return current stock with optional quantity-unit and location enrichment.

        The product object is always included (already embedded in the /stock response).
        Pass include_quantity_unit=True or include_location=True to attach the matching
        quantity_unit or location dict to each entry. Enrichment data is fetched in
        parallel with the main stock request and joined in Python by
        product.qu_id_stock / product.location_id.
        """
        coros = [client.get("/stock")]
        if include_quantity_unit:
            coros.append(client.get("/objects/quantity_units"))
        if include_location:
            coros.append(client.get("/objects/locations"))

        responses = await asyncio.gather(*coros)
        for r in responses:
            r.raise_for_status()

        stock_data: list[dict[str, Any]] = responses[0].json()

        qu_map: dict[int, dict[str, Any]] = {}
        loc_map: dict[int, dict[str, Any]] = {}

        idx = 1
        if include_quantity_unit:
            qu_map = {int(qu["id"]): qu for qu in responses[idx].json()}
            idx += 1
        if include_location:
            loc_map = {int(loc["id"]): loc for loc in responses[idx].json()}

        result = []
        for entry in stock_data:
            product = entry.get("product") or {}
            qu_id_raw = product.get("qu_id_stock")
            loc_id_raw = product.get("location_id")
            qu_id = int(qu_id_raw) if qu_id_raw is not None else None
            loc_id = int(loc_id_raw) if loc_id_raw is not None else None
            result.append(
                StockEnrichedEntry(
                    product_id=entry["product_id"],
                    amount=entry["amount"],
                    amount_aggregated=entry["amount_aggregated"],
                    amount_opened=entry["amount_opened"],
                    best_before_date=entry.get("best_before_date"),
                    is_aggregated_amount=entry["is_aggregated_amount"],
                    product=product,
                    quantity_unit=qu_map.get(qu_id) if qu_id is not None else None,
                    location=loc_map.get(loc_id) if loc_id is not None else None,
                )
            )
        return result

    @mcp.tool()
    async def add_stock(items: list[AddItem]) -> list[StockOpResult]:
        """Add stock for multiple products in one call.

        Each item fires a POST /stock/products/{id}/add. Results are collected
        concurrently; a failure on one item does not affect others.

        Each result includes transaction_id (for per-operation undo via the
        undo_transaction tool), amount_delta (net change from the stock log), and
        new_amount (resulting stock, fetched via an extra GET per product after the add).
        """

        async def _one(i: int, item: AddItem) -> StockOpResult:
            try:
                body: dict[str, Any] = {"amount": item.amount}
                if item.best_before_date is not None:
                    body["best_before_date"] = item.best_before_date
                if item.price is not None:
                    body["price"] = item.price
                if item.location_id is not None:
                    body["location_id"] = item.location_id
                if item.note is not None:
                    body["note"] = item.note

                r = await client.post(f"/stock/products/{item.product_id}/add", json=body)
                r.raise_for_status()
                entries: list[dict[str, Any]] = r.json()
                tx_id = entries[0]["transaction_id"] if entries else None
                amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None

                stock_r = await client.get(f"/stock/products/{item.product_id}")
                stock_r.raise_for_status()
                new_amount = float(stock_r.json().get("stock_amount", 0))

                return StockOpResult(
                    index=i, ok=True, transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount
                )
            except Exception as e:
                return StockOpResult(index=i, ok=False, error=str(e))

        return list(await asyncio.gather(*[_one(i, item) for i, item in enumerate(items)]))

    @mcp.tool()
    async def consume_stock(items: list[ConsumeItem]) -> list[StockOpResult]:
        """Consume stock for multiple products in one call.

        Each item fires a POST /stock/products/{id}/consume. Results are
        collected concurrently; a failure on one item does not affect others.

        Each result includes transaction_id, amount_delta (typically negative),
        and new_amount (resulting stock after the consume).
        """

        async def _one(i: int, item: ConsumeItem) -> StockOpResult:
            try:
                body: dict[str, Any] = {
                    "amount": item.amount,
                    "spoiled": item.spoiled,
                    "allow_subproduct_substitution": item.allow_subproduct_substitution,
                }
                if item.location_id is not None:
                    body["location_id"] = item.location_id

                r = await client.post(f"/stock/products/{item.product_id}/consume", json=body)
                r.raise_for_status()
                entries: list[dict[str, Any]] = r.json()
                tx_id = entries[0]["transaction_id"] if entries else None
                amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None

                stock_r = await client.get(f"/stock/products/{item.product_id}")
                stock_r.raise_for_status()
                new_amount = float(stock_r.json().get("stock_amount", 0))

                return StockOpResult(
                    index=i, ok=True, transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount
                )
            except Exception as e:
                return StockOpResult(index=i, ok=False, error=str(e))

        return list(await asyncio.gather(*[_one(i, item) for i, item in enumerate(items)]))

    @mcp.tool()
    async def inventory_products(items: list[InventoryItem]) -> list[StockOpResult]:
        """Set absolute stock amounts for multiple products in one call.

        Each item fires a POST /stock/products/{id}/inventory. Grocy computes how
        much to add or remove to reach new_amount and applies it atomically per product.
        Results are collected concurrently.

        Semantics of optional fields:
        - best_before_date, location_id, price apply ONLY to units being added. If Grocy
          removes units (because current stock > new_amount), existing entries keep their
          original dates, locations, and prices.
        - Omitting location_id uses the product's configured default location for added units.
        - Omitting best_before_date leaves added units without a due date.
        - Omitting price uses the product's last recorded price for added units.
        - Each item produces its own transaction_id; there is no cross-item atomicity — a
          failure on one product does not roll back others.
        """

        async def _one(i: int, item: InventoryItem) -> StockOpResult:
            try:
                body: dict[str, Any] = {"new_amount": item.new_amount}
                if item.best_before_date is not None:
                    body["best_before_date"] = item.best_before_date
                if item.location_id is not None:
                    body["location_id"] = item.location_id
                if item.price is not None:
                    body["price"] = item.price

                r = await client.post(f"/stock/products/{item.product_id}/inventory", json=body)
                r.raise_for_status()
                entries: list[dict[str, Any]] = r.json()
                tx_id = entries[0]["transaction_id"] if entries else None
                amount_delta = sum(float(e.get("amount", 0)) for e in entries) if entries else None

                stock_r = await client.get(f"/stock/products/{item.product_id}")
                stock_r.raise_for_status()
                new_amount = float(stock_r.json().get("stock_amount", 0))

                return StockOpResult(
                    index=i, ok=True, transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount
                )
            except Exception as e:
                return StockOpResult(index=i, ok=False, error=str(e))

        return list(await asyncio.gather(*[_one(i, item) for i, item in enumerate(items)]))
