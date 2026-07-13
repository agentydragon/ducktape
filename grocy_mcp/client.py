"""Request-scoped Grocy HTTP client with entity-name resolution.

``GrocyClient`` is the sole backend client used by both generated OpenAPI
tools and handwritten batch tools. Entity lookups therefore cannot drift from
the HTTP client's authentication or lifetime.

**No caching.** Every entity operation re-fetches the relevant
``/objects/<entity>`` list. Grocy has other writers (its UI, mobile clients,
and other agents), so longer-lived reference data could become stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

import httpx

from grocy_mcp.grocy_types import EntityType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolved:
    """A resolved entity reference: guaranteed-valid (id, name) pair."""

    id: int
    name: str


@dataclass(frozen=True)
class ResolvedQU:
    """A resolved QU reference with optional conversion to stock QU."""

    id: int
    name: str
    stock_qu_id: int
    stock_qu_name: str
    conversion_factor: float
    """Factor to multiply input amount by to get stock QU amount.
    1.0 when the input QU is the stock QU."""


class GrocyClient(httpx.AsyncClient):
    """HTTP client plus Grocy-specific entity operations.

    Entity methods use collision-safe names rather than a separate resolver
    namespace: ``get_entity`` remains distinct from the inherited HTTP
    ``get`` method, for example.
    """

    async def _fetch_entities(
        self, entity_type: EntityType
    ) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
        entity_path = f"/objects/{entity_type.value}"
        entity_label = entity_type.name.lower().replace("_", " ")
        response = await self.get(entity_path)
        response.raise_for_status()
        rows: list[dict[str, Any]] = response.json()
        by_id = {int(row["id"]): row for row in rows}
        by_name: dict[str, dict[str, Any]] = {}
        for row in rows:
            name_lower = str(row["name"]).lower()
            if name_lower in by_name:
                logger.warning(
                    "duplicate %s name %r (IDs: %d, %d)",
                    entity_label,
                    row["name"],
                    by_name[name_lower]["id"],
                    row["id"],
                )
            by_name[name_lower] = row
        all_names = sorted({str(row["name"]) for row in rows})
        return by_id, by_name, all_names

    async def resolve_entity(self, entity_type: EntityType, ref: int | str) -> Resolved:
        """Resolve an ID or name to a validated ``(id, name)`` pair."""
        entity_label = entity_type.name.lower().replace("_", " ")
        by_id, by_name, all_names = await self._fetch_entities(entity_type)

        if isinstance(ref, int):
            if (row := by_id.get(ref)) is None:
                raise ValueError(f"No {entity_label} with id={ref}. Available: {all_names}")
            return Resolved(id=int(row["id"]), name=str(row["name"]))

        if (row := by_name.get(ref.lower())) is not None:
            return Resolved(id=int(row["id"]), name=str(row["name"]))

        close = get_close_matches(ref.lower(), list(by_name.keys()), n=5, cutoff=0.4)
        suggestions = [str(by_name[match]["name"]) for match in close] if close else all_names[:10]
        raise ValueError(f"No {entity_label} named {ref!r}. Similar: {suggestions}")

    async def entity_name(self, entity_type: EntityType, entity_id: int) -> str:
        """Look up a name by ID, returning ``id=N`` when it is absent."""
        by_id, _, _ = await self._fetch_entities(entity_type)
        row = by_id.get(entity_id)
        return str(row["name"]) if row else f"id={entity_id}"

    async def get_entity(self, entity_type: EntityType, entity_id: int) -> dict[str, Any] | None:
        """Return one raw entity row by ID."""
        by_id, _, _ = await self._fetch_entities(entity_type)
        return by_id.get(entity_id)

    async def list_entities(self, entity_type: EntityType) -> list[dict[str, Any]]:
        """Return all raw rows of an entity type."""
        by_id, _, _ = await self._fetch_entities(entity_type)
        return list(by_id.values())

    async def entity_name_map(self, entity_type: EntityType) -> dict[int, str]:
        """Return ``{id: name}`` for an entity type."""
        by_id, _, _ = await self._fetch_entities(entity_type)
        return {entity_id: str(row["name"]) for entity_id, row in by_id.items()}

    async def _fetch_conversions(self) -> list[dict[str, Any]]:
        response = await self.get("/objects/quantity_unit_conversions_resolved")
        response.raise_for_status()
        rows: list[dict[str, Any]] = response.json()
        return rows

    async def resolve_qu_for_product(self, qu_ref: int | str, product_id: int) -> ResolvedQU:
        """Resolve a QU and validate or convert it for a product's stock QU."""
        resolved = await self.resolve_entity(EntityType.QUANTITY_UNIT, qu_ref)
        product = await self.get_entity(EntityType.PRODUCT, product_id)
        if product is None:
            raise ValueError(f"Product id={product_id} not found")

        stock_qu_id = int(product["qu_id_stock"])
        stock_qu_name = await self.entity_name(EntityType.QUANTITY_UNIT, stock_qu_id)
        if resolved.id == stock_qu_id:
            return ResolvedQU(
                id=resolved.id,
                name=resolved.name,
                stock_qu_id=stock_qu_id,
                stock_qu_name=stock_qu_name,
                conversion_factor=1.0,
            )

        conversions = await self._fetch_conversions()
        factor: float | None = None
        for conversion in conversions:
            if int(conversion["from_qu_id"]) != resolved.id or int(conversion["to_qu_id"]) != stock_qu_id:
                continue
            conversion_product_id = conversion.get("product_id")
            if conversion_product_id is not None and int(conversion_product_id) == product_id:
                factor = float(conversion["factor"])
                break
            if conversion_product_id is None and factor is None:
                factor = float(conversion["factor"])

        if factor is not None:
            return ResolvedQU(
                id=resolved.id,
                name=resolved.name,
                stock_qu_id=stock_qu_id,
                stock_qu_name=stock_qu_name,
                conversion_factor=factor,
            )

        available_from_qus: set[str] = set()
        for conversion in conversions:
            if int(conversion["to_qu_id"]) != stock_qu_id:
                continue
            conversion_product_id = conversion.get("product_id")
            if conversion_product_id is None or int(conversion_product_id) == product_id:
                from_id = int(conversion["from_qu_id"])
                available_from_qus.add(await self.entity_name(EntityType.QUANTITY_UNIT, from_id))

        product_name = str(product["name"])
        raise ValueError(
            f"No conversion from {resolved.name!r} to stock QU {stock_qu_name!r} "
            f"for product {product_name!r}. "
            f"Use qu: {stock_qu_name!r} directly"
            + (f", or one of: {sorted(available_from_qus)}" if available_from_qus else "")
            + "."
        )
