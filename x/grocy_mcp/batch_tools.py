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
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from pydantic import Field
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from x.grocy_mcp.grocy_types import PRODUCT_WRITABLE_FIELDS, EntityType, ReadableEntityType
from x.grocy_mcp.mcp_types import (
    DETAIL_DESC,
    PRODUCT_DESC,
    QU_DESC,
    AddItem,
    BriefListItem,
    BriefQuantityUnit,
    ConsumeItem,
    CreateError,
    CreateItem,
    CreateLocationItem,
    CreateOk,
    CreateProductGroupItem,
    CreateProductItem,
    CreateQuantityUnitItem,
    CreateShoppingListItem,
    EditProductField,
    EditProductItem,
    EditShoppingListField,
    EditStockEntryField,
    EditStockEntryItem,
    FullLocation,
    FullProduct,
    FullProductGroup,
    FullQuantityUnit,
    FullShoppingList,
    GetError,
    GetOk,
    ServerSettings,
    SetItem,
    ShoppingItem,
    ShoppingListItemError,
    ShoppingListItemOk,
    StockEntry,
    StockEntryDetail,
    StockEntryError,
    StockEntryOk,
    StockOpError,
    StockOpOk,
)
from x.grocy_mcp.resolver import EntityResolver, ResolvedQU

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


# Grocy sentinel: this string stored in `best_before_date` means "never expires".
_NEVER_EXPIRES_BBD = "2999-12-31"


def _compute_default_bbd(product: dict[str, Any], *, is_freezer: bool) -> str:
    """Compute BBD from product settings, matching Grocy's documented intent.

    Grocy's per-column sentinels:
      ``-1`` = never expires (returned as ``2999-12-31``)
      ``0``  = unconfigured → today
      ``N > 0`` = today + N days

    We compute client-side instead of letting Grocy fill in the default
    because ``StockService::AddProduct`` in v4.6.0 has a bug in its
    freezer branch: it uses guard ``default_best_before_days_after_freezing >= -1``
    (true for unconfigured products whose freezing default is the schema
    default 0), ignoring ``default_best_before_days`` entirely and
    returning today. The non-freezer branch is correct, so we apply the
    freezer-branch logic here with the guard the transfer branch uses
    (``> 0 || == -1``) and fall through to ``default_best_before_days``
    when the product has no freezing-specific shelf life.
    """
    if is_freezer:
        days_after_freezing = int(product.get("default_best_before_days_after_freezing") or 0)
        if days_after_freezing == -1:
            return _NEVER_EXPIRES_BBD
        if days_after_freezing > 0:
            return (date.today() + timedelta(days=days_after_freezing)).isoformat()
    days = int(product.get("default_best_before_days") or 0)
    if days == -1:
        return _NEVER_EXPIRES_BBD
    return (date.today() + timedelta(days=days)).isoformat()


def _format_exc(e: Exception) -> str:
    """Format exception with full traceback for error reporting.

    On ``HTTPStatusError``, append Grocy's response body so the agent sees
    the actual failure reason (Grocy returns a JSON ``error_message`` on
    most 4xx/5xx); httpx's default error is just the status + URL.
    """
    tb = "".join(traceback.format_exception(e))
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text.strip()
        if body:
            return f"{tb}\nGrocy response body: {body[:1500]}"
    return tb


def _is_retryable(exc: BaseException) -> bool:
    """Whether an exception is transient and worth retrying."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429, 500, 502, 503, 504}


# ── Tool registration ────────────────────────────────────────────────────────


def register_batch_tools(mcp: FastMCP, client: httpx.AsyncClient, settings: ServerSettings) -> None:
    """Register custom batch tools on an existing FastMCP instance."""
    sem = asyncio.Semaphore(settings.max_concurrent_requests)
    max_batch = settings.max_batch_size
    resolver = EntityResolver(client)

    def _check_batch_size(items: list[Any] | set[Any], label: str) -> None:
        if len(items) > max_batch:
            raise ValueError(f"batch too large: {len(items)} {label} exceeds maximum of {max_batch}")

    async def _retry[T](fn: Callable[[], Awaitable[T]]) -> T:
        """Run fn under semaphore with tenacity retry on transient errors."""
        async with sem:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_retryable),
                stop=stop_after_attempt(1 + settings.max_retries),
                wait=wait_exponential(multiplier=settings.retry_base_delay, exp_base=2),
                reraise=True,
            ):
                with attempt:
                    return await fn()
        raise AssertionError("unreachable")

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _build_enrichment_maps() -> tuple[dict[int, dict[str, Any]], dict[int, str], dict[int, str]]:
        """One-shot reference-data fetch for `_enrich_stock_entry`.

        Each name/field lookup would otherwise re-fetch `/objects/<entity>`,
        which becomes O(rows * 4) when iterating through many stock entries.
        Caller fetches once, then calls `_enrich_stock_entry` in a loop with
        these maps — state can't mutate mid-call so the local maps are safe.
        """
        products_raw, qu_names, location_names = await asyncio.gather(
            resolver.all(EntityType.PRODUCT),
            resolver.name_map(EntityType.QUANTITY_UNIT),
            resolver.name_map(EntityType.LOCATION),
        )
        return ({int(p["id"]): p for p in products_raw}, qu_names, location_names)

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

        stock_r, qu_names, location_names = await asyncio.gather(
            _retry(lambda: client.get("/stock")),
            resolver.name_map(EntityType.QUANTITY_UNIT),
            resolver.name_map(EntityType.LOCATION),
        )
        stock_r.raise_for_status()
        stock_data: list[dict[str, Any]] = stock_r.json()

        product_ids: set[int] = set()
        for ref in products:
            resolved = await resolver.resolve(EntityType.PRODUCT, ref)
            product_ids.add(resolved.id)

        location_ids: set[int] = set()
        for ref in locations:
            resolved = await resolver.resolve(EntityType.LOCATION, ref)
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

            qu_name = _lookup_name(product, "qu_id_stock", qu_names)
            location_name = _lookup_name(product, "location_id", location_names)

            result.append(
                StockEntry(
                    product_id=pid,
                    product_name=str(product.get("name", f"product_id={pid}")),
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

    async def _stock_mutate(item: AddItem | ConsumeItem | SetItem) -> StockOpOk | StockOpError:
        """Shared implementation for stock_add / stock_consume / stock_set."""
        try:
            product = await resolver.resolve(EntityType.PRODUCT, item.product)
            location = await resolver.resolve(EntityType.LOCATION, item.location)
            qu_ref = item.qu
            if qu_ref is None:
                # SetItem-only zeroing-out shortcut: no unit needed since
                # the amount is 0. AddItem/ConsumeItem keep qu required.
                assert isinstance(item, SetItem)
                if item.new_amount != 0:
                    raise ValueError("`qu` is required unless `new_amount` is 0 (zeroing-out shortcut).")
                product_row = await resolver.get(EntityType.PRODUCT, product.id)
                assert product_row is not None  # just resolved
                stock_qu_id = int(product_row["qu_id_stock"])
                stock_qu_name = await resolver.name(EntityType.QUANTITY_UNIT, stock_qu_id)
                rqu = ResolvedQU(
                    id=stock_qu_id,
                    name=stock_qu_name,
                    stock_qu_id=stock_qu_id,
                    stock_qu_name=stock_qu_name,
                    conversion_factor=1.0,
                )
            else:
                rqu = await resolver.resolve_qu_for_product(qu_ref, product.id)
            input_amount = item.new_amount if isinstance(item, SetItem) else item.amount
            stock_amount = input_amount * rqu.conversion_factor

            body: dict[str, Any] = {"location_id": location.id}
            match item:
                case AddItem():
                    endpoint = "add"
                    body["amount"] = stock_amount
                    if item.best_before_date is not None:
                        body["best_before_date"] = _date_to_str(item.best_before_date)
                    else:
                        # Compute the default client-side: the tool contract says
                        # omitting `best_before_date` uses the product's shelf-life
                        # defaults, but Grocy's native default-filling has a
                        # freezer-branch bug (see `_compute_default_bbd`).
                        product_row = await resolver.get(EntityType.PRODUCT, product.id)
                        location_row = await resolver.get(EntityType.LOCATION, location.id)
                        assert product_row is not None  # just resolved
                        assert location_row is not None  # just resolved
                        body["best_before_date"] = _compute_default_bbd(
                            product_row, is_freezer=bool(int(location_row.get("is_freezer") or 0))
                        )
                    if item.price is not None:
                        body["price"] = item.price
                    if item.note is not None:
                        body["note"] = item.note
                case ConsumeItem():
                    endpoint = "consume"
                    body["amount"] = stock_amount
                    if item.spoiled:
                        body["spoiled"] = True
                    if item.allow_subproduct_substitution:
                        body["allow_subproduct_substitution"] = True
                case SetItem():
                    endpoint = "inventory"
                    body["new_amount"] = stock_amount
                    if item.best_before_date is not None:
                        body["best_before_date"] = _date_to_str(item.best_before_date)
                    if item.price is not None:
                        body["price"] = item.price

            async def _do_post() -> tuple[str | None, float | None]:
                r = await client.post(f"/stock/products/{product.id}/{endpoint}", json=body)
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
        return list(await asyncio.gather(*[_stock_mutate(item) for item in items]))

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
        return list(await asyncio.gather(*[_stock_mutate(item) for item in items]))

    @mcp.tool()
    async def stock_set(items: list[SetItem]) -> list[StockOpOk | StockOpError]:
        """Set absolute stock amounts for one or more products. Max 20 items per call.

        Use this for corrections — "we actually have 10 kg of rice, not 3"
        — when you'd otherwise have to compute the delta yourself for
        `stock_add` / `stock_consume`. Grocy figures out whether to add
        or remove to reach `new_amount`. `best_before_date` and `price`
        apply only to units being added; ignored when removing.

        `qu` is optional when `new_amount` is 0: the unit is irrelevant
        when you're just zeroing stock out, so you can omit it and skip
        the per-product `stock_qu` lookup (useful for bulk "empty this
        location" operations). For any nonzero amount, `qu` is required.

        Each success carries a `transaction_id` for `transaction_undo`.
        See also `stock_add`, `stock_consume`, `stock_transfer`.
        """
        _check_batch_size(items, "items")
        return list(await asyncio.gather(*[_stock_mutate(item) for item in items]))

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
                resolved = await resolver.resolve(EntityType.PRODUCT, product_ref)
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
        products_by_id, qu_names, location_names = await _build_enrichment_maps()

        async def _one(entry_id: int) -> StockEntryOk | StockEntryError:
            try:

                async def _do() -> StockEntryOk:
                    r = await client.get(f"/stock/entry/{entry_id}")
                    r.raise_for_status()
                    detail = await _enrich_stock_entry(
                        r.json(), products_by_id=products_by_id, qu_names=qu_names, location_names=location_names
                    )
                    return StockEntryOk(entry=detail)

                return await _retry(_do)
            except Exception as e:
                return StockEntryError(entry_id=entry_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_one(eid) for eid in entry_ids]))

    @mcp.tool()
    async def stock_entry_edit(items: list[EditStockEntryItem]) -> list[StockEntryOk | StockEntryError]:
        """Partial update of one or more stock entries. Max 20 items per call.

        For each item, the server reads the current entry, merges your
        changes, and writes back — you don't have to copy-paste unchanged
        fields. Returns one result per input item, in order. Each success
        carries the post-edit entry plus a `changes` diff. To remove a
        nullable field's value (vs setting it to a new value), name it in
        `clear_fields`. See also `stock_entries_list` to discover IDs.
        """
        _check_batch_size(items, "items")

        async def _edit_one(item: EditStockEntryItem) -> StockEntryOk | StockEntryError:
            try:
                # Read current entry
                entry_r = await client.get(f"/stock/entry/{item.entry_id}")
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
                if item.amount is not None:
                    body["amount"] = item.amount
                if item.best_before_date is not None:
                    body["best_before_date"] = _date_to_str(item.best_before_date)
                if item.purchased_date is not None:
                    body["purchased_date"] = _date_to_str(item.purchased_date)
                if item.price is not None:
                    body["price"] = item.price
                if item.location is not None:
                    resolved_loc = await resolver.resolve(EntityType.LOCATION, item.location)
                    body["location_id"] = resolved_loc.id
                if item.open is not None:
                    body["open"] = item.open
                if item.note is not None:
                    body["note"] = item.note

                for field_name in item.clear_fields or ():
                    if field_name == EditStockEntryField.PRICE:
                        body["price"] = None
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
                r = await client.put(f"/stock/entry/{item.entry_id}", json=body)
                r.raise_for_status()

                # Re-fetch to return updated state
                updated_r = await client.get(f"/stock/entry/{item.entry_id}")
                updated_r.raise_for_status()
                detail = await _enrich_stock_entry(updated_r.json())
                return StockEntryOk(entry=detail, changes=changes or None)
            except Exception as e:
                return StockEntryError(entry_id=item.entry_id, error=_format_exc(e))

        return list(await asyncio.gather(*[_edit_one(item) for item in items]))

    # ── Reference data ───────────────────────────────────────────────────

    @mcp.tool()
    async def products_list(
        detail: Annotated[Literal["brief", "full"], Field(description=DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullProduct]:
        """Returns every product defined in this Grocy instance. Create new ones with `products_create`.

        Most tools accept product names directly, so you usually only
        need this when you want the full catalogue or the `full` shape
        (default location, stock QU, etc.).
        """
        rows = await resolver.all(EntityType.PRODUCT)
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullProduct.model_validate(r) for r in rows]

    @mcp.tool()
    async def locations_list(
        detail: Annotated[Literal["brief", "full"], Field(description=DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullLocation]:
        """Returns every storage location defined in this Grocy instance. Create new ones with `locations_create`.

        Stock operations accept location names directly; use this when
        you want to see what already exists before `products_create` or
        `stock_add`.
        """
        rows = await resolver.all(EntityType.LOCATION)
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullLocation.model_validate(r) for r in rows]

    @mcp.tool()
    async def quantity_units_list(
        detail: Annotated[Literal["brief", "full"], Field(description=DETAIL_DESC)] = "brief",
    ) -> list[BriefQuantityUnit] | list[FullQuantityUnit]:
        """Returns every quantity unit defined in this Grocy instance. Create new ones with `quantity_units_create`.

        Grocy ships with only `Piece` pre-defined; any unit the agent
        needs (Kilogram, Liter, Bag, …) has to be created first. Every
        stock operation needs a `qu`; check here when in doubt. For
        conversions between units, list `quantity_unit_conversions` via
        `entities_list`.
        """
        rows = await resolver.all(EntityType.QUANTITY_UNIT)
        if detail == "brief":
            return [
                BriefQuantityUnit(id=int(r["id"]), name=str(r["name"]), name_plural=r.get("name_plural")) for r in rows
            ]
        return [FullQuantityUnit.model_validate(r) for r in rows]

    @mcp.tool()
    async def product_groups_list(
        detail: Annotated[Literal["brief", "full"], Field(description=DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullProductGroup]:
        """Returns every product-group (category) defined in this Grocy instance.

        Pass product-group names or IDs to `products_create` /
        `products_edit`. Create new ones with `product_groups_create`.
        """
        rows = await resolver.all(EntityType.PRODUCT_GROUP)
        if detail == "brief":
            return [BriefListItem(id=int(r["id"]), name=str(r["name"])) for r in rows]
        return [FullProductGroup.model_validate(r) for r in rows]

    @mcp.tool()
    async def shopping_lists_list(
        detail: Annotated[Literal["brief", "full"], Field(description=DETAIL_DESC)] = "brief",
    ) -> list[BriefListItem] | list[FullShoppingList]:
        """Returns every shopping list defined in this Grocy instance.

        This is the list-metadata table (one row per named list like
        "Weekly" or "Costco run"), *not* the items on those lists —
        for items use `shopping_list_get`. Create new lists with
        `shopping_lists_create`.
        """
        rows = await resolver.all(EntityType.SHOPPING_LIST)
        if detail == "brief":
            return [BriefListItem(id=int(row["id"]), name=str(row["name"])) for row in rows]
        return [FullShoppingList.model_validate(row) for row in rows]

    # ── Reference data creation ──────────────────────────────────────────

    async def _simple_batch_create[T](
        items: list[T], entity_path: str, to_body: Callable[[T], dict[str, Any]]
    ) -> list[CreateOk | CreateError]:
        """Shared implementation for simple entity creation tools."""
        _check_batch_size(items, "items")

        async def _one(item: T) -> CreateOk | CreateError:
            try:

                async def _do() -> CreateOk:
                    r = await client.post(entity_path, json=to_body(item))
                    r.raise_for_status()
                    raw_id = r.json().get("created_object_id")
                    return CreateOk(created_object_id=int(raw_id) if raw_id is not None else None)

                return await _retry(_do)
            except Exception as e:
                return CreateError(error=_format_exc(e))

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def locations_create(items: list[CreateLocationItem]) -> list[CreateOk | CreateError]:
        """Create one or more storage locations. Max 20 items per call.

        Locations are referenced by name everywhere else (stock ops,
        product creation, etc.) — pick stable, distinctive names. Use
        `locations_list` to discover what already exists. Failed items
        return errors without aborting the others.
        """

        def _body(item: CreateLocationItem) -> dict[str, Any]:
            body = item.model_dump(exclude_none=True)
            body["is_freezer"] = int(body["is_freezer"])  # Grocy wants 0/1
            return body

        return await _simple_batch_create(items, "/objects/locations", _body)

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
        return await _simple_batch_create(
            items, "/objects/quantity_units", lambda item: item.model_dump(exclude_none=True)
        )

    @mcp.tool()
    async def product_groups_create(items: list[CreateProductGroupItem]) -> list[CreateOk | CreateError]:
        """Create one or more product groups (categories). Max 20 items per call.

        Categorising products is optional in Grocy; these groups show up as
        the `product_group` reference on products. Use `product_groups_list`
        to discover existing groups. Failed items return errors without
        aborting the others.
        """
        return await _simple_batch_create(
            items, "/objects/product_groups", lambda item: item.model_dump(exclude_none=True)
        )

    @mcp.tool()
    async def shopping_lists_create(items: list[CreateShoppingListItem]) -> list[CreateOk | CreateError]:
        """Create one or more shopping lists (metadata). Max 20 items per call.

        This creates the list itself, not items on it — items go through
        `shopping_list_items_add`. Pass the returned list name or ID as
        the `shopping_list` argument to every shopping-list tool. Use
        `shopping_lists_list` to discover existing lists. Failed items
        return errors without aborting the others.
        """
        return await _simple_batch_create(
            items, "/objects/shopping_lists", lambda item: item.model_dump(exclude_none=True)
        )

    # ── Product management ───────────────────────────────────────────────

    @mcp.tool()
    async def products_create(items: list[CreateProductItem]) -> list[CreateOk | CreateError]:
        """Create one or more products. Max 20 items per call.

        Each item needs `name`, `stock_qu`, and `location`. All entity
        references (`stock_qu`, `location`, `purchase_qu`, `product_group`)
        take names or IDs and resolve via `quantity_units_list` /
        `locations_list` / `product_groups_list`. `purchase_qu` defaults to
        `stock_qu` when omitted. When `purchase_qu` differs from
        `stock_qu`, Grocy auto-creates a product-specific factor=1
        `quantity_unit_conversions` row (unless a matching conversion
        already exists, e.g. from a global default). Adjust it via
        `entities_list` / `entity_update` on `quantity_unit_conversions`
        if the real factor is not 1. Failed items return errors without
        aborting the others.

        Pair with `locations_create` and `quantity_units_create` to bring
        up a fresh Grocy instance from scratch; use `products_edit` /
        `product_delete` for mutations after creation.
        """
        _check_batch_size(items, "items")

        async def _one(item: CreateProductItem) -> CreateOk | CreateError:
            try:
                loc = await resolver.resolve(EntityType.LOCATION, item.location)
                squ = await resolver.resolve(EntityType.QUANTITY_UNIT, item.stock_qu)
                pqu = (
                    await resolver.resolve(EntityType.QUANTITY_UNIT, item.purchase_qu)
                    if item.purchase_qu is not None
                    else squ
                )

                cqu = (
                    await resolver.resolve(EntityType.QUANTITY_UNIT, item.consume_qu)
                    if item.consume_qu is not None
                    else squ
                )

                body: dict[str, Any] = {
                    "name": item.name,
                    "location_id": loc.id,
                    "qu_id_stock": squ.id,
                    "qu_id_purchase": pqu.id,
                    "qu_id_consume": cqu.id,
                    "min_stock_amount": item.min_stock_amount,
                    "default_best_before_days": item.default_best_before_days,
                    "due_type": item.due_type,
                }
                if item.parent_product is not None:
                    parent = await resolver.resolve(EntityType.PRODUCT, item.parent_product)
                    body["parent_product_id"] = parent.id
                if item.product_group is not None:
                    pg = await resolver.resolve(EntityType.PRODUCT_GROUP, item.product_group)
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
    async def products_edit(items: list[EditProductItem]) -> list[CreateOk | CreateError]:
        """Partial update of one or more products. Max 20 items per call.

        Only the fields you set on each item change — the rest are
        preserved. The server reads each product, merges your changes,
        and writes back. To null out a nullable field (vs setting it to
        a new value), name it in `clear_fields`. Failed items return
        errors without aborting the others. See also `products_create`,
        `product_delete`, `products_list`.
        """
        _check_batch_size(items, "items")

        async def _one(item: EditProductItem) -> CreateOk | CreateError:
            try:
                resolved = await resolver.resolve(EntityType.PRODUCT, item.product)
                r = await client.get(f"/objects/products/{resolved.id}")
                r.raise_for_status()
                current: dict[str, Any] = r.json()

                body = {k: v for k, v in current.items() if k in PRODUCT_WRITABLE_FIELDS}
                if item.name is not None:
                    body["name"] = item.name
                if item.stock_qu is not None:
                    body["qu_id_stock"] = (await resolver.resolve(EntityType.QUANTITY_UNIT, item.stock_qu)).id
                if item.location is not None:
                    body["location_id"] = (await resolver.resolve(EntityType.LOCATION, item.location)).id
                if item.purchase_qu is not None:
                    body["qu_id_purchase"] = (await resolver.resolve(EntityType.QUANTITY_UNIT, item.purchase_qu)).id
                if item.consume_qu is not None:
                    body["qu_id_consume"] = (await resolver.resolve(EntityType.QUANTITY_UNIT, item.consume_qu)).id
                if item.min_stock_amount is not None:
                    body["min_stock_amount"] = item.min_stock_amount
                if item.default_best_before_days is not None:
                    body["default_best_before_days"] = item.default_best_before_days
                if item.due_type is not None:
                    body["due_type"] = item.due_type
                if item.parent_product is not None:
                    body["parent_product_id"] = (await resolver.resolve(EntityType.PRODUCT, item.parent_product)).id
                if item.product_group is not None:
                    body["product_group_id"] = (await resolver.resolve(EntityType.PRODUCT_GROUP, item.product_group)).id
                if item.description is not None:
                    body["description"] = item.description

                for field in item.clear_fields or ():
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

        return list(await asyncio.gather(*[_one(item) for item in items]))

    @mcp.tool()
    async def product_delete(
        product: Annotated[int | str, Field(description="Product to delete. Name or ID.")],
    ) -> CreateOk | CreateError:
        """Delete a product. Also removes every stock entry for the product — irreversible."""
        try:
            resolved = await resolver.resolve(EntityType.PRODUCT, product)
            r = await client.delete(f"/objects/products/{resolved.id}")
            r.raise_for_status()
            return CreateOk(created_object_id=resolved.id)
        except Exception as e:
            return CreateError(error=_format_exc(e))

    # ── Transfer stock ───────────────────────────────────────────────────

    @mcp.tool()
    async def stock_transfer(
        product: Annotated[int | str, Field(description=PRODUCT_DESC)],
        amount: Annotated[float, Field(description="Amount to transfer, in `qu` units.")],
        qu: Annotated[int | str, Field(description=QU_DESC)],
        from_location: Annotated[int | str, Field(description="Source location. Name or ID.")],
        to_location: Annotated[int | str, Field(description="Destination location. Name or ID.")],
    ) -> StockOpOk | StockOpError:
        """Move stock from one location to another (e.g. Cellar → Fridge).

        Stock totals don't change; only the per-location split does.
        **Freezer warning**: transferring to/from a freezer location
        silently changes the best-before date (using the product's
        `default_best_before_days_after_freezing` /
        `default_best_before_days_after_thawing`). The result's
        `amount_delta` and `new_amount` are null for transfers — Grocy
        doesn't return them. The `transaction_id` works with
        `transaction_undo` to revert. See also `stock_add`,
        `stock_consume`, `stock_set`.
        """
        try:
            prod = await resolver.resolve(EntityType.PRODUCT, product)
            from_loc = await resolver.resolve(EntityType.LOCATION, from_location)
            to_loc = await resolver.resolve(EntityType.LOCATION, to_location)
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

    async def _shopping_product_info(product_id: Any) -> tuple[str | None, str | None]:
        """Look up product name and stock QU name for a shopping-list item's product_id."""
        if product_id is None:
            return None, None
        product = await resolver.get(EntityType.PRODUCT, int(product_id))
        if product is None:
            return f"id={product_id}", None
        qu_name = await resolver.name(EntityType.QUANTITY_UNIT, int(product["qu_id_stock"]))
        return str(product["name"]), qu_name

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
        sl = await resolver.resolve(EntityType.SHOPPING_LIST, shopping_list)

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
            product_name, qu_name = await _shopping_product_info(product_id)

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
                sl = await resolver.resolve(EntityType.SHOPPING_LIST, item.shopping_list)
                body: dict[str, Any] = {"shopping_list_id": sl.id, "amount": item.amount}
                product_name = None
                qu_name = None
                if item.product is not None:
                    prod = await resolver.resolve(EntityType.PRODUCT, item.product)
                    body["product_id"] = prod.id
                    product_name, qu_name = await _shopping_product_info(prod.id)
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
            for field in clear_fields or ():
                if field == EditShoppingListField.NOTE:
                    body["note"] = None

            r = await client.put(f"/objects/shopping_list/{item_id}", json=body)
            r.raise_for_status()

            product_name, qu_name = await _shopping_product_info(body.get("product_id"))

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

                product_name, qu_name = await _shopping_product_info(item.get("product_id"))

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
        sl = await resolver.resolve(EntityType.SHOPPING_LIST, shopping_list)

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

    async def _fetch_volatile_with_maps() -> tuple[dict[str, Any], dict[int, str], dict[int, str]]:
        """Fetch ``/stock/volatile`` and build QU + location name maps in one shot."""
        r = await _retry(lambda: client.get("/stock/volatile"))
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        qu_names, location_names = await asyncio.gather(
            resolver.name_map(EntityType.QUANTITY_UNIT), resolver.name_map(EntityType.LOCATION)
        )
        return data, qu_names, location_names

    def _lookup_name(product: dict[str, Any], key: str, name_map: dict[int, str]) -> str:
        raw_id = product.get(key)
        if raw_id is None:
            raise ValueError(f"product {product.get('id')} has no {key}")
        return name_map[int(raw_id)]

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
        data, qu_names, location_names = await _fetch_volatile_with_maps()
        cutoff = datetime.now(tz=UTC).date() + timedelta(days=days_ahead)
        result = []

        for entry in data.get("due_products", []):
            product = entry.get("product") or {}
            expiry_date = _parse_date(entry.get("best_before_date"))
            if expiry_date is None or expiry_date > cutoff:
                continue

            result.append(
                {
                    "product_id": int(entry["product_id"]),
                    "product_name": str(product.get("name", "")),
                    "amount": float(entry.get("amount", 0)),
                    "qu_name": _lookup_name(product, "qu_id_stock", qu_names),
                    "location_name": _lookup_name(product, "location_id", location_names),
                    "best_before_date": entry.get("best_before_date"),
                    "days_until_expiry": (expiry_date - datetime.now(tz=UTC).date()).days,
                }
            )
        return result

    @mcp.tool()
    async def get_below_minimum_stock() -> list[dict[str, Any]]:
        """Products under their `min_stock_amount` threshold.

        Each row gives product name, current amount, minimum amount,
        unit, and `deficit` (how much is missing). Products with
        `min_stock_amount = 0` never appear here — set the threshold
        via `products_create` / `products_edit`. See also
        `get_expiring_stock` and `get_expired_stock`.
        """
        data, qu_names, _ = await _fetch_volatile_with_maps()

        result = []
        for entry in data.get("missing_products", []):
            product = entry.get("product") or {}
            amount_missing = float(entry.get("amount_missing", 0))
            min_amount = float(product.get("min_stock_amount", 0))
            current = min_amount - amount_missing if amount_missing > 0 else 0

            result.append(
                {
                    "product_id": int(entry.get("id", 0)),
                    "product_name": str(product.get("name", "")),
                    "amount": current,
                    "min_amount": min_amount,
                    "qu_name": _lookup_name(product, "qu_id_stock", qu_names),
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
        data, qu_names, location_names = await _fetch_volatile_with_maps()

        result = []
        for entry in data.get("expired_products", []):
            product = entry.get("product") or {}
            expiry_date = _parse_date(entry.get("best_before_date"))
            days_overdue = (datetime.now(tz=UTC).date() - expiry_date).days if expiry_date else 0

            result.append(
                {
                    "product_id": int(entry.get("product_id", 0)),
                    "product_name": str(product.get("name", "")),
                    "amount": float(entry.get("amount", 0)),
                    "qu_name": _lookup_name(product, "qu_id_stock", qu_names),
                    "location_name": _lookup_name(product, "location_id", location_names),
                    "best_before_date": entry.get("best_before_date"),
                    "days_overdue": days_overdue,
                }
            )
        return result
