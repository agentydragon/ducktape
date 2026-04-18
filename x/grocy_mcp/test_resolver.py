"""Unit tests for EntityResolver."""

from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest
import pytest_bazel
import respx

from x.grocy_mcp.resolver import EntityResolver, Resolved, ResolvedQU

BASE_URL = "http://grocy.test/api"

PRODUCTS = [
    {"id": 1, "name": "Rice", "qu_id_stock": 3, "location_id": 1},
    {"id": 2, "name": "Milk", "qu_id_stock": 5, "location_id": 2},
]

LOCATIONS = [{"id": 1, "name": "Pantry"}, {"id": 2, "name": "Fridge"}, {"id": 3, "name": "Freezer"}]

QUS = [
    {"id": 1, "name": "Piece"},
    {"id": 3, "name": "Kilogram"},
    {"id": 5, "name": "Liter"},
    {"id": 7, "name": "Crate"},
]

CONVERSIONS = [
    # Crate -> Kilogram, global, factor 24
    {"id": 1, "from_qu_id": 7, "to_qu_id": 3, "factor": 24.0, "product_id": None},
    # Liter -> Kilogram, product-specific for Rice (id=1), factor 0.8
    {"id": 2, "from_qu_id": 5, "to_qu_id": 3, "factor": 0.8, "product_id": 1},
]


def _setup_routes(router: respx.Router) -> None:
    """Register standard Grocy API mock routes on the given router."""
    router.get("/objects/products").respond(json=PRODUCTS)
    router.get("/objects/locations").respond(json=LOCATIONS)
    router.get("/objects/quantity_units").respond(json=QUS)
    router.get("/objects/product_groups").respond(json=[])
    router.get("/objects/shopping_lists").respond(json=[{"id": 1, "name": "Shopping list"}])
    router.get("/objects/quantity_unit_conversions_resolved").respond(json=CONVERSIONS)


@pytest.fixture
def mock_router() -> Generator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        _setup_routes(router)
        yield router


@pytest.fixture
def resolver(mock_router: respx.MockRouter) -> EntityResolver:
    client = httpx.AsyncClient(base_url=BASE_URL)
    return EntityResolver(client)


# -- Product resolution ----------------------------------------------------


async def test_resolve_product_by_id(resolver: EntityResolver) -> None:
    result = await resolver.resolve_product(1)
    assert result == Resolved(id=1, name="Rice")


async def test_resolve_product_by_name(resolver: EntityResolver) -> None:
    result = await resolver.resolve_product("Rice")
    assert result == Resolved(id=1, name="Rice")


async def test_resolve_product_by_name_case_insensitive(resolver: EntityResolver) -> None:
    result = await resolver.resolve_product("rice")
    assert result == Resolved(id=1, name="Rice")


async def test_resolve_product_unknown_id(resolver: EntityResolver) -> None:
    with pytest.raises(ValueError, match="No product with id=99"):
        await resolver.resolve_product(99)


async def test_resolve_product_unknown_name_suggests_similar(resolver: EntityResolver) -> None:
    with pytest.raises(ValueError, match=r"No product named 'Ric'.*Similar"):
        await resolver.resolve_product("Ric")


# -- Location resolution ---------------------------------------------------


async def test_resolve_location_by_name(resolver: EntityResolver) -> None:
    result = await resolver.resolve_location("Fridge")
    assert result == Resolved(id=2, name="Fridge")


async def test_resolve_location_unknown(resolver: EntityResolver) -> None:
    with pytest.raises(ValueError, match="No location named 'Garage'"):
        await resolver.resolve_location("Garage")


# -- QU resolution ---------------------------------------------------------


async def test_resolve_qu_by_name(resolver: EntityResolver) -> None:
    result = await resolver.resolve_qu("Kilogram")
    assert result == Resolved(id=3, name="Kilogram")


async def test_resolve_qu_by_id(resolver: EntityResolver) -> None:
    result = await resolver.resolve_qu(5)
    assert result == Resolved(id=5, name="Liter")


# -- QU for product: direct match ------------------------------------------


async def test_qu_for_product_direct_match(resolver: EntityResolver) -> None:
    """Rice's stock QU is Kilogram (id=3). Passing Kilogram -> factor 1.0."""
    result = await resolver.resolve_qu_for_product("Kilogram", product_id=1)
    assert result == ResolvedQU(id=3, name="Kilogram", stock_qu_id=3, stock_qu_name="Kilogram", conversion_factor=1.0)


# -- QU for product: global conversion -------------------------------------


async def test_qu_for_product_global_conversion(resolver: EntityResolver) -> None:
    """Crate -> Kilogram has a global conversion (factor 24). Works for Rice."""
    result = await resolver.resolve_qu_for_product("Crate", product_id=1)
    assert result == ResolvedQU(id=7, name="Crate", stock_qu_id=3, stock_qu_name="Kilogram", conversion_factor=24.0)


# -- QU for product: product-specific conversion ----------------------------


async def test_qu_for_product_specific_conversion(resolver: EntityResolver) -> None:
    """Liter -> Kilogram has a product-specific conversion for Rice (factor 0.8)."""
    result = await resolver.resolve_qu_for_product("Liter", product_id=1)
    assert result == ResolvedQU(id=5, name="Liter", stock_qu_id=3, stock_qu_name="Kilogram", conversion_factor=0.8)


# -- QU for product: no conversion -----------------------------------------


async def test_qu_for_product_no_conversion(resolver: EntityResolver) -> None:
    """Milk's stock QU is Liter (id=5). Piece has no conversion to Liter."""
    with pytest.raises(ValueError, match=r"No conversion from 'Piece' to stock QU 'Liter'.*Milk"):
        await resolver.resolve_qu_for_product("Piece", product_id=2)


# -- QU for product: product-specific overrides global ----------------------


async def test_product_specific_conversion_overrides_global() -> None:
    """When both global and product-specific conversions exist, product-specific wins."""
    conversions_with_global = [
        *CONVERSIONS,
        # Global Liter -> Kilogram with factor 1.0 (product-specific is 0.8)
        {"id": 3, "from_qu_id": 5, "to_qu_id": 3, "factor": 1.0, "product_id": None},
    ]

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/objects/products").respond(json=PRODUCTS)
        router.get("/objects/quantity_units").respond(json=QUS)
        router.get("/objects/quantity_unit_conversions_resolved").respond(json=conversions_with_global)

        client = httpx.AsyncClient(base_url=BASE_URL)
        resolver = EntityResolver(client)

        # Should use product-specific factor (0.8), not global (1.0)
        result = await resolver.resolve_qu_for_product("Liter", product_id=1)
        assert result.conversion_factor == 0.8


# -- Name lookups ----------------------------------------------------------


async def test_product_name_lookup(resolver: EntityResolver) -> None:
    assert await resolver.product_name(1) == "Rice"
    assert await resolver.product_name(999) == "id=999"


async def test_location_name_lookup(resolver: EntityResolver) -> None:
    assert await resolver.location_name(2) == "Fridge"


async def test_qu_name_lookup(resolver: EntityResolver) -> None:
    assert await resolver.qu_name(3) == "Kilogram"


# -- Freshness -------------------------------------------------------------


async def test_resolver_fetches_fresh_per_call() -> None:
    """Resolver is stateless: every lookup re-fetches from Grocy.

    The MCP server isn't the only client of a Grocy instance, so we
    deliberately don't cache — a cache would hide state another client
    changed behind us. One `/objects/products` GET per resolve is the
    correct, intended cost.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        products_route = router.get("/objects/products").respond(json=PRODUCTS)
        router.get("/objects/quantity_units").respond(json=QUS)

        client = httpx.AsyncClient(base_url=BASE_URL)
        resolver = EntityResolver(client)

        await resolver.resolve_product("Rice")
        await resolver.resolve_product("Milk")
        await resolver.resolve_product(1)

        assert products_route.call_count == 3


if __name__ == "__main__":
    pytest_bazel.main()
