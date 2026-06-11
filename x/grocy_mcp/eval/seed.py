"""Initial-state seeders for eval cases.

Each seeder populates a fresh Grocy instance over the REST API (same
endpoints the MCP server hits) so that when the agent takes over, the
pantry, fridge, freezer, and shopping list reflect a lived-in household
rather than an empty install.

A seeder is an `async def seed(client: httpx.AsyncClient) -> None` where
`client.base_url` is the Grocy `/api` root. Auth is disabled in the eval
container, so no Authorization header is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── REST helpers ────────────────────────────────────────────────────────


async def _create(client: httpx.AsyncClient, entity: str, body: dict[str, Any]) -> int:
    r = await client.post(f"/objects/{entity}", json=body)
    r.raise_for_status()
    return int(r.json()["created_object_id"])


async def _lookup_by_name(client: httpx.AsyncClient, entity: str, name: str) -> int | None:
    """Return the id of an entity by case-insensitive name, or None."""
    r = await client.get(f"/objects/{entity}")
    r.raise_for_status()
    for row in r.json():
        if str(row["name"]).lower() == name.lower():
            return int(row["id"])
    return None


async def _get_or_create_location(client: httpx.AsyncClient, name: str, *, is_freezer: bool = False) -> int:
    if (existing := await _lookup_by_name(client, "locations", name)) is not None:
        return existing
    return await _create(client, "locations", {"name": name, "is_freezer": int(is_freezer)})


async def _get_or_create_qu(client: httpx.AsyncClient, name: str, name_plural: str | None = None) -> int:
    if (existing := await _lookup_by_name(client, "quantity_units", name)) is not None:
        return existing
    return await _create(client, "quantity_units", {"name": name, "name_plural": name_plural or f"{name}s"})


async def _get_or_create_product_group(client: httpx.AsyncClient, name: str) -> int:
    if (existing := await _lookup_by_name(client, "product_groups", name)) is not None:
        return existing
    return await _create(client, "product_groups", {"name": name})


async def _create_product(
    client: httpx.AsyncClient,
    *,
    name: str,
    qu_id: int,
    location_id: int,
    group_id: int | None = None,
    min_stock_amount: float = 0,
) -> int:
    body: dict[str, Any] = {
        "name": name,
        "qu_id_stock": qu_id,
        "qu_id_purchase": qu_id,
        "location_id": location_id,
        "min_stock_amount": min_stock_amount,
    }
    if group_id is not None:
        body["product_group_id"] = group_id
    return await _create(client, "products", body)


async def _add_stock(
    client: httpx.AsyncClient,
    product_id: int,
    amount: float,
    *,
    best_before_date: str = "2099-12-31",
    location_id: int | None = None,
) -> None:
    body: dict[str, Any] = {"amount": amount, "best_before_date": best_before_date, "transaction_type": "purchase"}
    if location_id is not None:
        body["location_id"] = location_id
    r = await client.post(f"/stock/products/{product_id}/add", json=body)
    r.raise_for_status()


async def _add_to_shopping_list(
    client: httpx.AsyncClient,
    *,
    product_id: int | None = None,
    amount: float = 1,
    note: str | None = None,
    list_id: int = 1,
) -> None:
    body: dict[str, Any] = {"shopping_list_id": list_id, "amount": amount}
    if product_id is not None:
        body["product_id"] = product_id
    if note is not None:
        body["note"] = note
    r = await client.post("/objects/shopping_list", json=body)
    r.raise_for_status()


# ── Shared scaffolding (locations, QUs, product groups) ─────────────────


@dataclass
class _Base:
    """IDs for the baseline entities every seeded case uses."""

    locations: dict[str, int] = field(default_factory=dict)
    qus: dict[str, int] = field(default_factory=dict)
    groups: dict[str, int] = field(default_factory=dict)


async def _seed_base(client: httpx.AsyncClient) -> _Base:
    """Seed locations, QUs, and product groups.

    Grocy's LinuxServer image pre-seeds "Fridge" (location) and "Piece" +
    "Pack" (QUs), so helpers below are get-or-create to tolerate those.
    """
    base = _Base()
    base.locations["Pantry"] = await _get_or_create_location(client, "Pantry")
    base.locations["Fridge"] = await _get_or_create_location(client, "Fridge")
    base.locations["Freezer"] = await _get_or_create_location(client, "Freezer", is_freezer=True)

    for name, plural in (
        ("Piece", "Pieces"),
        ("Kilogram", "Kilograms"),
        ("Gram", "Grams"),
        ("Liter", "Liters"),
        ("Milliliter", "Milliliters"),
        ("Jar", "Jars"),
        ("Bottle", "Bottles"),
        ("Can", "Cans"),
        ("Box", "Boxes"),
        ("Pack", "Packs"),
        ("Bulb", "Bulbs"),
        ("Stalk", "Stalks"),
        ("Bag", "Bags"),
    ):
        base.qus[name] = await _get_or_create_qu(client, name, plural)

    for name in ("Vegetables", "Meat", "Dairy", "Pantry", "Frozen", "Condiments"):
        base.groups[name] = await _get_or_create_product_group(client, name)

    return base


async def _seed_background_pantry(client: httpx.AsyncClient, base: _Base) -> None:
    """Realistic background stock that both scenarios share. None of these
    are part of the bolognese recipe; they make the scene look lived-in
    and serve as negative examples (the agent should not touch them).
    """
    pantry = base.locations["Pantry"]
    fridge = base.locations["Fridge"]
    freezer = base.locations["Freezer"]
    kg = base.qus["Kilogram"]
    g = base.qus["Gram"]
    l_ = base.qus["Liter"]
    piece = base.qus["Piece"]
    pack = base.qus["Pack"]
    box = base.qus["Box"]
    jar = base.qus["Jar"]
    bottle = base.qus["Bottle"]
    pantry_g = base.groups["Pantry"]
    dairy_g = base.groups["Dairy"]
    frozen_g = base.groups["Frozen"]
    veg_g = base.groups["Vegetables"]

    # (name, qu, location, group, stock_amount)
    items = [
        ("Rice", kg, pantry, pantry_g, 2.0),
        ("Oats", g, pantry, pantry_g, 500.0),
        ("Sugar", kg, pantry, pantry_g, 1.0),
        ("Flour", kg, pantry, pantry_g, 1.0),
        ("Eggs", piece, fridge, dairy_g, 6.0),
        ("Butter", g, fridge, dairy_g, 250.0),
        ("Plain yoghurt", l_, fridge, dairy_g, 1.0),
        ("Milk", l_, fridge, dairy_g, 2.0),
        ("Orange juice", l_, fridge, dairy_g, 1.0),
        ("Frozen spinach", pack, freezer, frozen_g, 1.0),
        ("Frozen peas", pack, freezer, frozen_g, 1.0),
        ("Frozen pizza", box, freezer, frozen_g, 1.0),
        ("Apples", piece, fridge, veg_g, 5.0),
        ("Bananas", piece, pantry, veg_g, 4.0),
        ("Peanut butter", jar, pantry, pantry_g, 1.0),
        ("Coffee beans", g, pantry, pantry_g, 500.0),
        ("Tea bags", box, pantry, pantry_g, 1.0),
        ("Dish soap", bottle, pantry, None, 1.0),
    ]
    for name, qu, loc, group, amount in items:
        pid = await _create_product(client, name=name, qu_id=qu, location_id=loc, group_id=group)
        await _add_stock(client, pid, amount)


# ── Case seed ──────────────────────────────────────────────────────────


async def seed_lived_in_pantry(client: httpx.AsyncClient) -> None:
    """Realistic household pantry used by both the shopping-planning and
    post-cook-logging cases.

    The state is cookable — all carbonara ingredients are on hand — while
    still leaving a meaningful shopping gap for the planning case:
    `Ground beef`, `Tomato passata`, `Red wine`, and `Carrot` are missing,
    and `Tomato paste` / `Celery` / `Parmesan` sit at or below their
    minimums. A background of ~18 unrelated household items serves as
    negative examples (the agent should not touch them).

    Also seeds three items on Grocy's default shopping list (Milk, Bread,
    Dish sponges) so the planning case exercises `shopping_list_get`
    before appending anything.
    """
    base = await _seed_base(client)
    await _seed_background_pantry(client, base)

    pantry = base.locations["Pantry"]
    fridge = base.locations["Fridge"]
    kg = base.qus["Kilogram"]
    g = base.qus["Gram"]
    l_ = base.qus["Liter"]
    piece = base.qus["Piece"]
    bulb = base.qus["Bulb"]
    stalk = base.qus["Stalk"]
    jar = base.qus["Jar"]
    bottle = base.qus["Bottle"]
    pantry_g = base.groups["Pantry"]
    cond_g = base.groups["Condiments"]
    dairy_g = base.groups["Dairy"]
    veg_g = base.groups["Vegetables"]
    meat_g = base.groups["Meat"]

    # Stocked carbonara-capable pantry. Amounts chosen so the post-cook
    # case has a plausible carbonara consumption path (pancetta → 0,
    # eggs 6 → 3, parmesan 200g → 100g, spaghetti 500g → 300g).
    stocked = [
        ("Spaghetti", g, pantry, pantry_g, 500.0, 0.0),
        ("Pancetta", g, fridge, meat_g, 150.0, 0.0),
        ("Parmesan", g, fridge, dairy_g, 200.0, 0.0),
        ("Olive oil", l_, pantry, cond_g, 1.0, 0.0),
        ("Salt", kg, pantry, cond_g, 1.0, 0.0),
        ("Black pepper", g, pantry, cond_g, 100.0, 0.0),
        ("Bay leaves", g, pantry, cond_g, 20.0, 0.0),
        ("Onion", piece, fridge, veg_g, 2.0, 0.0),
        ("Garlic", bulb, fridge, veg_g, 1.0, 0.0),
    ]
    for name, qu, loc, group, amount, min_s in stocked:
        pid = await _create_product(
            client, name=name, qu_id=qu, location_id=loc, group_id=group, min_stock_amount=min_s
        )
        await _add_stock(client, pid, amount)

    # Low / borderline — bolognese ingredients present in small amounts
    # with `min_stock_amount` set so `get_below_minimum_stock` flags them.
    low = [("Tomato paste", g, pantry, cond_g, 100.0, 200.0), ("Celery", stalk, fridge, veg_g, 1.0, 3.0)]
    for name, qu, loc, group, amount, min_s in low:
        pid = await _create_product(
            client, name=name, qu_id=qu, location_id=loc, group_id=group, min_stock_amount=min_s
        )
        await _add_stock(client, pid, amount)

    # Missing — product exists (so the agent can target it by name) but
    # zero stock. min=0 so these don't also show as below-minimum; the
    # shopping-planning agent should derive them from reasoning about the
    # recipe, not from a red flag.
    missing = [
        ("Ground beef", g, fridge, meat_g),
        ("Tomato passata", jar, pantry, cond_g),
        ("Red wine", bottle, pantry, cond_g),
        ("Carrot", piece, fridge, veg_g),
    ]
    for name, qu, loc, group in missing:
        await _create_product(client, name=name, qu_id=qu, location_id=loc, group_id=group)

    # Pre-existing shopping list (Grocy ships with list id=1, "Shopping list").
    # Mix of product-linked and note-only items; exercises shopping_list_get
    # returning non-empty state before the agent appends anything.
    r = await client.get("/objects/products")
    r.raise_for_status()
    by_name = {str(p["name"]).lower(): int(p["id"]) for p in r.json()}
    await _add_to_shopping_list(client, product_id=by_name["milk"], amount=1)
    await _add_to_shopping_list(client, note="Bread", amount=1)
    await _add_to_shopping_list(client, note="Dish sponges", amount=1)
