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
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Self

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field, model_validator

from x.grocy_mcp.config import ServerSettings

logger = logging.getLogger(__name__)


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


# ── QU resolver ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedQU:
    qu_id: int
    qu_name: str


class QUCache:
    """Caches quantity unit data for the duration of a single batch call."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._by_id: dict[int, dict[str, Any]] | None = None
        self._by_name: dict[str, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        if self._by_id is not None:
            return
        async with self._lock:
            if self._by_id is not None:
                return
            r = await self._client.get("/objects/quantity_units")
            r.raise_for_status()
            qus: list[dict[str, Any]] = r.json()
            self._by_id = {int(qu["id"]): qu for qu in qus}
            self._by_name = {}
            for qu in qus:
                name = str(qu["name"]).lower()
                self._by_name.setdefault(name, []).append(qu)

    async def resolve(self, *, qu_id: int | None, qu_name: str | None) -> ResolvedQU:
        """Resolve qu_id or qu_name to a validated (qu_id, qu_name) pair."""
        await self._ensure_loaded()
        assert self._by_id is not None
        assert self._by_name is not None

        if qu_id is not None:
            qu = self._by_id.get(qu_id)
            if qu is None:
                available_ids = sorted(self._by_id.keys())
                raise ValueError(f"unknown qu_id={qu_id}; available IDs: {available_ids}")
            return ResolvedQU(qu_id=int(qu["id"]), qu_name=str(qu["name"]))

        assert qu_name is not None
        matches = self._by_name.get(qu_name.lower(), [])
        if not matches:
            available = sorted({str(qu["name"]) for qu in self._by_id.values()})
            raise ValueError(f"unknown qu_name={qu_name!r}; available names: {available}")
        if len(matches) > 1:
            ids = [int(qu["id"]) for qu in matches]
            raise ValueError(
                f"ambiguous qu_name={qu_name!r} matches {len(matches)} QUs (IDs: {ids}); use qu_id instead"
            )
        qu = matches[0]
        return ResolvedQU(qu_id=int(qu["id"]), qu_name=str(qu["name"]))

    async def validate_product_qu(self, product_id: int, resolved: ResolvedQU, product_data: dict[str, Any]) -> None:
        """Validate that the resolved QU matches the product's qu_id_stock."""
        expected_qu_id = int(product_data["qu_id_stock"])
        if resolved.qu_id != expected_qu_id:
            await self._ensure_loaded()
            assert self._by_id is not None
            expected_qu = self._by_id.get(expected_qu_id)
            expected_name = str(expected_qu["name"]) if expected_qu else f"qu_id={expected_qu_id}"
            raise ValueError(
                f"product {product_id} uses stock QU {expected_name!r} (qu_id={expected_qu_id}), "
                f"but got {resolved.qu_name!r} (qu_id={resolved.qu_id})"
            )

    async def get_qu_name(self, qu_id: int) -> str:
        """Look up QU name by ID."""
        await self._ensure_loaded()
        assert self._by_id is not None
        qu = self._by_id.get(qu_id)
        return str(qu["name"]) if qu else f"qu_id={qu_id}"


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
    # TODO: discriminated union per entity_type for body (avoids having to know per-entity field names)
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


class StockEnrichedEntry(BaseModel):
    product_id: int
    amount: float
    amount_aggregated: float
    amount_opened: float
    best_before_date: str | None = None
    is_aggregated_amount: bool
    qu_name: str
    product: dict[str, Any]
    quantity_unit: dict[str, Any] | None = None
    location: dict[str, Any] | None = None


class _QUIdentifier(BaseModel):
    """Mixin for models that require exactly one of qu_id or qu_name."""

    qu_id: int | None = Field(default=None, description="Quantity unit ID. Provide either qu_id or qu_name.")
    qu_name: str | None = Field(default=None, description="Quantity unit name. Provide either qu_id or qu_name.")

    @model_validator(mode="after")
    def _check_qu(self) -> Self:
        if self.qu_id is None and self.qu_name is None:
            raise ValueError("exactly one of qu_id or qu_name is required")
        if self.qu_id is not None and self.qu_name is not None:
            raise ValueError("provide only one of qu_id or qu_name, not both")
        return self


class AddItem(_QUIdentifier):
    product_id: int
    amount: float
    best_before_date: str | None = Field(default=None, description="ISO date. Omit → today.")
    price: float | None = Field(default=None, description="Omit → last recorded price for product.")
    location_id: int | None = Field(default=None, description="Omit → product's default location.")
    note: str | None = None


class ConsumeItem(_QUIdentifier):
    product_id: int
    amount: float
    spoiled: bool = False
    location_id: int | None = Field(
        default=None,
        description=(
            "Which location to consume from. Omit → consume from any location (Grocy picks). "
            "If the product has stock in multiple locations, specify location_id to be explicit."
        ),
    )
    allow_subproduct_substitution: bool = False


class InventoryItem(_QUIdentifier):
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


class StockOpOk(BaseModel):
    kind: Literal["ok"] = "ok"
    transaction_id: str | None = Field(default=None, description="Grocy transaction ID for per-operation undo.")
    amount_delta: float | None = Field(default=None, description="Net stock change applied (negative for consume).")
    new_amount: float | None = Field(
        default=None, description="Best-effort resulting stock amount. May be None if the follow-up read fails."
    )
    qu_name: str = Field(description="Name of the quantity unit for the amounts.")


class StockOpError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class StockEntryOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entry_id: int
    data: dict[str, Any]
    qu_name: str = Field(description="Name of the stock quantity unit for amounts in this entry.")


class StockEntryError(BaseModel):
    kind: Literal["error"] = "error"
    entry_id: int
    error: str


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

    # ── Entity CRUD ───────────────────────────────────────────────────────

    @mcp.tool()
    async def create_entities(items: list[CreateItem]) -> list[CreateOk | CreateError]:
        """Create multiple Grocy entities in one call. Maximum 20 items per call.

        Sends one POST /objects/{entity_type} per item concurrently (up to
        4 in parallel). Failed items are collected as CreateError; they do not
        abort others. Transient errors are retried.
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
        """Fetch multiple Grocy entity types in one call. Maximum 20 entity types per call.

        Returns a mapping of entity_type → list of entity objects, fetched
        concurrently (up to 4 in parallel). Raises on the first failed fetch
        (fail-fast). Use separate calls if partial failure tolerance is needed.
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
        """Fetch multiple Grocy objects of the same entity type by ID. Maximum 20 IDs per call.

        Returns one result per ID, each carrying entity_type and object_id for
        unambiguous identification without index-matching. Failed fetches are
        returned as GetError and do not abort others.
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

    # ── Stock overview ────────────────────────────────────────────────────

    @mcp.tool()
    async def get_stock(
        include_quantity_unit: bool = False, include_location: bool = False
    ) -> list[StockEnrichedEntry]:
        """Return current stock with quantity unit name and optional enrichment.

        qu_name is always included. Pass include_quantity_unit=True or
        include_location=True to attach the full quantity_unit or location dict.
        """
        qu_cache = QUCache(client)

        async def _get(path: str) -> httpx.Response:
            return await _retry(lambda: client.get(path))

        coros: list[Any] = [_get("/stock")]
        if include_quantity_unit:
            coros.append(_get("/objects/quantity_units"))
        if include_location:
            coros.append(_get("/objects/locations"))

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
            qu_name = await qu_cache.get_qu_name(qu_id) if qu_id is not None else "unknown"
            result.append(
                StockEnrichedEntry(
                    product_id=entry["product_id"],
                    amount=entry["amount"],
                    amount_aggregated=entry["amount_aggregated"],
                    amount_opened=entry["amount_opened"],
                    best_before_date=entry.get("best_before_date"),
                    is_aggregated_amount=entry["is_aggregated_amount"],
                    qu_name=qu_name,
                    product=product,
                    quantity_unit=qu_map.get(qu_id) if qu_id is not None else None,
                    location=loc_map.get(loc_id) if loc_id is not None else None,
                )
            )
        return result

    # ── Stock mutations ───────────────────────────────────────────────────

    async def _resolve_and_validate_qu(
        qu_cache: QUCache, item: _QUIdentifier, product_data: dict[str, Any]
    ) -> ResolvedQU:
        """Resolve qu_id/qu_name and validate against the product's stock QU."""
        resolved = await qu_cache.resolve(qu_id=item.qu_id, qu_name=item.qu_name)
        await qu_cache.validate_product_qu(item.product_id, resolved, product_data)  # type: ignore[attr-defined]
        return resolved

    async def _best_effort_new_amount(product_id: int) -> float | None:
        """Read current stock amount after a mutation. Best-effort, never retried."""
        try:
            stock_r = await client.get(f"/stock/products/{product_id}")
            stock_r.raise_for_status()
            return float(stock_r.json().get("stock_amount", 0))
        except Exception:
            logger.warning("failed to read new_amount for product %d after mutation", product_id)
            return None

    @mcp.tool()
    async def add_stock(items: list[AddItem]) -> list[StockOpOk | StockOpError]:
        """Add stock for multiple products in one call. Maximum 20 items per call.

        You must specify the quantity unit (qu_id or qu_name) for each item.
        The unit must match the product's stock quantity unit.

        Each result includes transaction_id (for undo), amount_delta, and
        new_amount (best-effort, may be None).
        """
        _check_batch_size(items, "items")
        qu_cache = QUCache(client)

        async def _one(item: AddItem) -> StockOpOk | StockOpError:
            try:
                # Fetch product to validate QU
                product_r = await client.get(f"/objects/products/{item.product_id}")
                product_r.raise_for_status()
                product_data = product_r.json()
                resolved = await _resolve_and_validate_qu(qu_cache, item, product_data)

                async def _do_post() -> tuple[str | None, float | None]:
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
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(item.product_id)
                return StockOpOk(
                    transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount, qu_name=resolved.qu_name
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def consume_stock(items: list[ConsumeItem]) -> list[StockOpOk | StockOpError]:
        """Consume stock for multiple products in one call. Maximum 20 items per call.

        You must specify the quantity unit (qu_id or qu_name) for each item.
        The unit must match the product's stock quantity unit.

        Each result includes transaction_id, amount_delta (typically negative),
        and new_amount (best-effort, may be None).
        """
        _check_batch_size(items, "items")
        qu_cache = QUCache(client)

        async def _one(item: ConsumeItem) -> StockOpOk | StockOpError:
            try:
                product_r = await client.get(f"/objects/products/{item.product_id}")
                product_r.raise_for_status()
                product_data = product_r.json()
                resolved = await _resolve_and_validate_qu(qu_cache, item, product_data)

                async def _do_post() -> tuple[str | None, float | None]:
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
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(item.product_id)
                return StockOpOk(
                    transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount, qu_name=resolved.qu_name
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def inventory_products(items: list[InventoryItem]) -> list[StockOpOk | StockOpError]:
        """Set absolute stock amounts for multiple products in one call. Maximum 20 items per call.

        You must specify the quantity unit (qu_id or qu_name) for each item.
        The unit must match the product's stock quantity unit.

        Grocy computes how much to add or remove to reach new_amount. Optional fields
        (best_before_date, location_id, price) apply only to units being added.
        """
        _check_batch_size(items, "items")
        qu_cache = QUCache(client)

        async def _one(item: InventoryItem) -> StockOpOk | StockOpError:
            try:
                product_r = await client.get(f"/objects/products/{item.product_id}")
                product_r.raise_for_status()
                product_data = product_r.json()
                resolved = await _resolve_and_validate_qu(qu_cache, item, product_data)

                async def _do_post() -> tuple[str | None, float | None]:
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
                    return tx_id, amount_delta

                tx_id, amount_delta = await _retry(_do_post)
                new_amount = await _best_effort_new_amount(item.product_id)
                return StockOpOk(
                    transaction_id=tx_id, amount_delta=amount_delta, new_amount=new_amount, qu_name=resolved.qu_name
                )
            except Exception as e:
                return StockOpError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    # ── Stock entry read/edit ─────────────────────────────────────────────

    @mcp.tool()
    async def get_stock_entries(entry_ids: list[int]) -> list[StockEntryOk | StockEntryError]:
        """Fetch multiple stock entries by ID. Maximum 20 IDs per call.

        Each entry is enriched with qu_name (the product's stock quantity unit name).
        """
        _check_batch_size(entry_ids, "entry_ids")
        qu_cache = QUCache(client)

        async def _one(entry_id: int) -> StockEntryOk | StockEntryError:
            try:

                async def _do() -> StockEntryOk:
                    r = await client.get(f"/stock/entry/{entry_id}")
                    r.raise_for_status()
                    data: dict[str, Any] = r.json()
                    product_id = int(data["product_id"])
                    # Fetch product to get qu_id_stock
                    product_r = await client.get(f"/objects/products/{product_id}")
                    product_r.raise_for_status()
                    product_data = product_r.json()
                    qu_id = int(product_data["qu_id_stock"])
                    qu_name = await qu_cache.get_qu_name(qu_id)
                    return StockEntryOk(entry_id=entry_id, data=data, qu_name=qu_name)

                return await _retry(_do)
            except Exception as e:
                return StockEntryError(entry_id=entry_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_one(eid) for eid in entry_ids]))

    @mcp.tool()
    async def edit_stock_entry(
        entry_id: int,
        amount: float,
        best_before_date: str,
        purchased_date: str,
        price: float,
        location_id: int,
        open: bool = False,
        qu_id: int | None = None,
        qu_name: str | None = None,
    ) -> StockEntryOk | StockEntryError:
        """Edit a stock entry. All fields are sent to Grocy (it requires the full object).

        You must specify the quantity unit (qu_id or qu_name) to confirm you know
        what unit the amount is in. The unit must match the product's stock QU.

        The `open` field defaults to False. Omitting it previously caused Grocy to crash
        (BoolToInt(null) in PHP).
        """
        if qu_id is None and qu_name is None:
            return StockEntryError(entry_id=entry_id, error="exactly one of qu_id or qu_name is required")
        if qu_id is not None and qu_name is not None:
            return StockEntryError(entry_id=entry_id, error="provide only one of qu_id or qu_name, not both")

        qu_cache = QUCache(client)
        try:
            resolved = await qu_cache.resolve(qu_id=qu_id, qu_name=qu_name)

            # Fetch current entry to get product_id for QU validation
            entry_r = await client.get(f"/stock/entry/{entry_id}")
            entry_r.raise_for_status()
            entry_data: dict[str, Any] = entry_r.json()
            product_id = int(entry_data["product_id"])

            product_r = await client.get(f"/objects/products/{product_id}")
            product_r.raise_for_status()
            product_data = product_r.json()
            await qu_cache.validate_product_qu(product_id, resolved, product_data)

            body: dict[str, Any] = {
                "amount": amount,
                "best_before_date": best_before_date,
                "purchased_date": purchased_date,
                "price": price,
                "location_id": location_id,
                "open": open,
            }
            r = await client.put(f"/stock/entry/{entry_id}", json=body)
            r.raise_for_status()

            # Re-fetch the entry to return the updated state
            updated_r = await client.get(f"/stock/entry/{entry_id}")
            updated_r.raise_for_status()
            return StockEntryOk(entry_id=entry_id, data=updated_r.json(), qu_name=resolved.qu_name)
        except Exception as e:
            return StockEntryError(entry_id=entry_id, error=_format_exc(e))
