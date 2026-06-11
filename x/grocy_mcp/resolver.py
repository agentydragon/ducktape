"""Entity name/ID resolver for Grocy MCP tools.

Resolves ``int | str`` references to Grocy entities (products, locations,
quantity units, product groups, shopping lists) into validated
``(id, name)`` pairs, parameterized by ``EntityType``.

**No caching.** Every resolution re-fetches the relevant ``/objects/<entity>``
list from Grocy. The MCP server isn't the only client of a Grocy
instance — assuming any longer-than-call lifetime would let other
clients (the web UI, mobile app, another agent) mutate state behind us
and we'd silently serve stale lookups. The cost is one extra fetch per
resolve; the simplicity wins.

A single ``EntityResolver`` is created once in ``register_batch_tools`` and
shared across every batch tool — since it's stateless, sharing is just
about avoiding the ``EntityResolver(client)`` boilerplate at each call
site.

Grocy enforces ``name TEXT NOT NULL UNIQUE`` on products, locations, and
quantity_units at the database level, so name-based resolution is
unambiguous by construction. Duplicate-name checks are retained as a
defensive measure in case Grocy relaxes this constraint.

QU conversion support: when a tool specifies a QU that differs from the
product's stock QU, the resolver looks up the conversion factor from
Grocy's ``quantity_unit_conversions_resolved`` entity and returns it.
Grocy's stock API only accepts amounts in the stock QU, so the caller
must multiply by the conversion factor before sending to Grocy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

import httpx

from x.grocy_mcp.grocy_types import EntityType

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


class EntityResolver:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _fetch(
        self, entity_type: EntityType
    ) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
        entity_path = f"/objects/{entity_type.value}"
        entity_label = entity_type.name.lower().replace("_", " ")
        r = await self._client.get(entity_path)
        r.raise_for_status()
        rows: list[dict[str, Any]] = r.json()
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

    async def resolve(self, entity_type: EntityType, ref: int | str) -> Resolved:
        """Resolve an int (ID) or str (name) to a validated (id, name) pair."""
        entity_label = entity_type.name.lower().replace("_", " ")
        by_id, by_name, all_names = await self._fetch(entity_type)

        if isinstance(ref, int):
            if (row := by_id.get(ref)) is None:
                raise ValueError(f"No {entity_label} with id={ref}. Available: {all_names}")
            return Resolved(id=int(row["id"]), name=str(row["name"]))

        if (row := by_name.get(ref.lower())) is not None:
            return Resolved(id=int(row["id"]), name=str(row["name"]))

        close = get_close_matches(ref.lower(), list(by_name.keys()), n=5, cutoff=0.4)
        suggestions = [str(by_name[m]["name"]) for m in close] if close else all_names[:10]
        raise ValueError(f"No {entity_label} named {ref!r}. Similar: {suggestions}")

    async def name(self, entity_type: EntityType, entity_id: int) -> str:
        """Look up name by ID. Returns 'id=N' if not found."""
        by_id, _, _ = await self._fetch(entity_type)
        row = by_id.get(entity_id)
        return str(row["name"]) if row else f"id={entity_id}"

    async def get(self, entity_type: EntityType, entity_id: int) -> dict[str, Any] | None:
        """Get the raw entity dict by ID."""
        by_id, _, _ = await self._fetch(entity_type)
        return by_id.get(entity_id)

    async def all(self, entity_type: EntityType) -> list[dict[str, Any]]:
        """Return all rows for an entity type."""
        by_id, _, _ = await self._fetch(entity_type)
        return list(by_id.values())

    async def name_map(self, entity_type: EntityType) -> dict[int, str]:
        """Return ``{id: name}`` for every row of an entity type.

        Useful for batch enrichment where per-row ``name()`` calls would
        re-fetch the same table O(N) times.
        """
        by_id, _, _ = await self._fetch(entity_type)
        return {eid: str(row["name"]) for eid, row in by_id.items()}

    # ── QU validation with conversion support ────────────────────────

    async def _fetch_conversions(self) -> list[dict[str, Any]]:
        r = await self._client.get("/objects/quantity_unit_conversions_resolved")
        r.raise_for_status()
        rows: list[dict[str, Any]] = r.json()
        return rows

    async def resolve_qu_for_product(self, qu_ref: int | str, product_id: int) -> ResolvedQU:
        """Resolve a QU reference and validate it against a product's stock QU.

        If the QU matches the stock QU, returns conversion_factor=1.0.
        If a conversion exists (product-specific or global), returns the factor.
        Otherwise raises ValueError with the stock QU name and available conversions.
        """
        resolved = await self.resolve(EntityType.QUANTITY_UNIT, qu_ref)
        product = await self.get(EntityType.PRODUCT, product_id)
        if product is None:
            raise ValueError(f"Product id={product_id} not found")

        stock_qu_id = int(product["qu_id_stock"])
        stock_qu_name = await self.name(EntityType.QUANTITY_UNIT, stock_qu_id)

        # Direct match — no conversion needed
        if resolved.id == stock_qu_id:
            return ResolvedQU(
                id=resolved.id,
                name=resolved.name,
                stock_qu_id=stock_qu_id,
                stock_qu_name=stock_qu_name,
                conversion_factor=1.0,
            )

        # Look for a conversion
        conversions = await self._fetch_conversions()

        factor: float | None = None
        for conv in conversions:
            if int(conv["from_qu_id"]) != resolved.id or int(conv["to_qu_id"]) != stock_qu_id:
                continue
            conv_product_id = conv.get("product_id")
            if conv_product_id is not None and int(conv_product_id) == product_id:
                # Product-specific conversion takes priority
                factor = float(conv["factor"])
                break
            if conv_product_id is None and factor is None:
                # Global conversion as fallback
                factor = float(conv["factor"])

        if factor is not None:
            return ResolvedQU(
                id=resolved.id,
                name=resolved.name,
                stock_qu_id=stock_qu_id,
                stock_qu_name=stock_qu_name,
                conversion_factor=factor,
            )

        # No conversion found — error with helpful info
        available_from_qus: set[str] = set()
        for conv in conversions:
            if int(conv["to_qu_id"]) == stock_qu_id:
                cp = conv.get("product_id")
                if cp is None or int(cp) == product_id:
                    from_id = int(conv["from_qu_id"])
                    from_name = await self.name(EntityType.QUANTITY_UNIT, from_id)
                    available_from_qus.add(from_name)

        product_name = str(product["name"])
        raise ValueError(
            f"No conversion from {resolved.name!r} to stock QU {stock_qu_name!r} "
            f"for product {product_name!r}. "
            f"Use qu: {stock_qu_name!r} directly"
            + (f", or one of: {sorted(available_from_qus)}" if available_from_qus else "")
            + "."
        )
