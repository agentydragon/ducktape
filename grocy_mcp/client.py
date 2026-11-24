"""Request-scoped Grocy HTTP client with entity-name resolution.

``GrocyClient`` is the sole backend client used by both generated OpenAPI
tools and handwritten batch tools. Entity lookups therefore cannot drift from
the HTTP client's authentication or lifetime.

Every entity operation fetches fresh reference data from Grocy, which also
has other writers. Quantity-unit resolution uses one unit list for the
input, stock unit, and conversion suggestions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

import httpx2

from grocy_mcp.grocy_types import EntityType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedQU:
    """A resolved QU reference with optional conversion to stock QU."""

    name: str
    stock_qu_name: str | None
    """Stock unit when the input unit differs, including one-to-one conversions."""
    conversion_factor: float
    """Factor to multiply input amount by to get stock QU amount.
    1.0 when the input QU is the stock QU."""


def _entity_name(by_id: dict[int, dict[str, Any]], entity_id: int) -> str:
    row = by_id.get(entity_id)
    return str(row["name"]) if row else f"id={entity_id}"


def resolve_entity_id(entity_type: EntityType, ref: int | str, by_id: dict[int, dict[str, Any]]) -> int:
    """Resolve against the supplied rows, using the same names and suggestions as the HTTP client."""
    entity_label = entity_type.name.lower().replace("_", " ")
    if isinstance(ref, int):
        if ref in by_id:
            return ref
        all_names = sorted({str(row["name"]) for row in by_id.values()})
        raise ValueError(f"No {entity_label} with id={ref}. Available: {all_names}")

    by_name: dict[str, dict[str, Any]] = {}
    for row in by_id.values():
        name_lower = str(row["name"]).lower()
        if name_lower in by_name:
            logger.warning(
                "duplicate %s name %r (IDs: %d, %d)", entity_label, row["name"], by_name[name_lower]["id"], row["id"]
            )
        by_name[name_lower] = row
    if (match := by_name.get(ref.lower())) is not None:
        return int(match["id"])

    close = get_close_matches(ref.lower(), list(by_name), n=5, cutoff=0.4)
    suggestions = [str(by_name[match]["name"]) for match in close]
    if not suggestions:
        suggestions = sorted({str(row["name"]) for row in by_id.values()})[:10]
    raise ValueError(f"No {entity_label} named {ref!r}. Similar: {suggestions}")


class GrocyClient(httpx2.AsyncClient):
    """HTTP client for Grocy entities, stock units, and conversions."""

    async def entities_by_id(self, entity_type: EntityType) -> dict[int, dict[str, Any]]:
        response = await self.get(f"/objects/{entity_type}")
        response.raise_for_status()
        rows: list[dict[str, Any]] = response.json()
        return {int(row["id"]): row for row in rows}

    async def resolve_entity(self, entity_type: EntityType, ref: int | str) -> dict[str, Any]:
        """Return the full catalog row for an ID or case-insensitive name."""
        rows = await self.entities_by_id(entity_type)
        return rows[resolve_entity_id(entity_type, ref, rows)]

    async def entity_name_map(self, entity_type: EntityType) -> dict[int, str]:
        """Return ``{id: name}`` for an entity type."""
        by_id = await self.entities_by_id(entity_type)
        return {entity_id: str(row["name"]) for entity_id, row in by_id.items()}

    async def _fetch_conversions(self) -> list[dict[str, Any]]:
        response = await self.get("/objects/quantity_unit_conversions_resolved")
        response.raise_for_status()
        rows: list[dict[str, Any]] = response.json()
        return rows

    async def resolve_qu_for_product(self, qu_ref: int | str | None, product: dict[str, Any]) -> ResolvedQU:
        """Resolve a QU and validate or convert it for a product's stock QU."""
        units = await self.entities_by_id(EntityType.QUANTITY_UNIT)
        stock_qu_id = int(product["qu_id_stock"])
        stock_qu_name = _entity_name(units, stock_qu_id)
        unit_id = stock_qu_id if qu_ref is None else resolve_entity_id(EntityType.QUANTITY_UNIT, qu_ref, units)
        if unit_id == stock_qu_id:
            return ResolvedQU(name=stock_qu_name, stock_qu_name=None, conversion_factor=1.0)

        unit_name = str(units[unit_id]["name"])
        product_id = int(product["id"])
        conversions = await self._fetch_conversions()
        factor: float | None = None
        for conversion in conversions:
            if int(conversion["from_qu_id"]) != unit_id or int(conversion["to_qu_id"]) != stock_qu_id:
                continue
            conversion_product_id = conversion.get("product_id")
            if conversion_product_id is not None and int(conversion_product_id) == product_id:
                factor = float(conversion["factor"])
                break
            if conversion_product_id is None and factor is None:
                factor = float(conversion["factor"])

        if factor is not None:
            return ResolvedQU(name=unit_name, stock_qu_name=stock_qu_name, conversion_factor=factor)

        available_from_qus: set[str] = set()
        for conversion in conversions:
            if int(conversion["to_qu_id"]) != stock_qu_id:
                continue
            conversion_product_id = conversion.get("product_id")
            if conversion_product_id is None or int(conversion_product_id) == product_id:
                from_id = int(conversion["from_qu_id"])
                available_from_qus.add(_entity_name(units, from_id))

        product_name = str(product["name"])
        raise ValueError(
            f"No conversion from {unit_name!r} to stock QU {stock_qu_name!r} "
            f"for product {product_name!r}. "
            f"Use qu: {stock_qu_name!r} directly"
            + (f", or one of: {sorted(available_from_qus)}" if available_from_qus else "")
            + "."
        )
