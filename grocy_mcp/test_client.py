"""Unit tests for GrocyClient entity operations."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_bazel
import respx

from grocy_mcp.client import GrocyClient, ResolvedQU
from grocy_mcp.grocy_types import EntityType

BASE_URL = "http://grocy.test/api"
# Not every test calls every route _setup_routes registers.
pytestmark = pytest.mark.httpx2(base_url=BASE_URL, assert_all_called=False)

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
def mock_router(httpx2_mock: respx.Router) -> respx.Router:
    router = httpx2_mock
    _setup_routes(router)
    return router


@pytest.fixture
async def grocy_client(mock_router: respx.MockRouter) -> AsyncIterator[GrocyClient]:
    async with GrocyClient(base_url=BASE_URL) as client:
        yield client


@pytest.mark.parametrize(
    ("entity_type", "ref", "expected"),
    [
        (EntityType.PRODUCT, 1, PRODUCTS[0]),
        (EntityType.PRODUCT, "rice", PRODUCTS[0]),
        (EntityType.LOCATION, "Fridge", LOCATIONS[1]),
        (EntityType.QUANTITY_UNIT, "Kilogram", QUS[1]),
        (EntityType.QUANTITY_UNIT, 5, QUS[2]),
    ],
    ids=["product-id", "product-name-case-insensitive", "location-name", "qu-name", "qu-id"],
)
async def test_resolve_entity(
    grocy_client: GrocyClient, entity_type: EntityType, ref: int | str, expected: dict[str, object]
) -> None:
    assert await grocy_client.resolve_entity(entity_type, ref) == expected


async def test_resolve_product_unknown_id(grocy_client: GrocyClient) -> None:
    with pytest.raises(ValueError, match="99") as exc_info:
        await grocy_client.resolve_entity(EntityType.PRODUCT, 99)
    assert "Milk" in str(exc_info.value)
    assert "Rice" in str(exc_info.value)


async def test_resolve_product_unknown_name_suggests_similar(grocy_client: GrocyClient) -> None:
    # Only the close match is offered: the fallback would list every product.
    with pytest.raises(ValueError, match=r"\bRic\b") as exc_info:
        await grocy_client.resolve_entity(EntityType.PRODUCT, "Ric")
    assert "Rice" in str(exc_info.value)
    assert "Milk" not in str(exc_info.value)


async def test_resolve_location_unknown(grocy_client: GrocyClient) -> None:
    with pytest.raises(ValueError, match="Garage"):
        await grocy_client.resolve_entity(EntityType.LOCATION, "Garage")


async def test_qu_for_product_global_conversion(grocy_client: GrocyClient) -> None:
    """Crate -> Kilogram has a global conversion (factor 24). Works for Rice."""
    result = await grocy_client.resolve_qu_for_product("Crate", PRODUCTS[0])
    assert result == ResolvedQU(name="Crate", stock_qu_name="Kilogram", conversion_factor=24.0)


async def test_qu_for_product_specific_conversion(grocy_client: GrocyClient) -> None:
    """Liter -> Kilogram has a product-specific conversion for Rice (factor 0.8)."""
    result = await grocy_client.resolve_qu_for_product("Liter", PRODUCTS[0])
    assert result == ResolvedQU(name="Liter", stock_qu_name="Kilogram", conversion_factor=0.8)


async def test_qu_for_product_no_conversion(grocy_client: GrocyClient) -> None:
    """Milk's stock QU is Liter (id=5). Piece has no conversion to Liter."""
    with pytest.raises(ValueError, match="Piece") as exc_info:
        await grocy_client.resolve_qu_for_product("Piece", PRODUCTS[1])
    assert "Liter" in str(exc_info.value)
    assert "Milk" in str(exc_info.value)


async def test_qu_conversion_suggestions_use_the_same_unit_listing(
    grocy_client: GrocyClient, mock_router: respx.Router
) -> None:
    units_route = mock_router.get("/objects/quantity_units").respond(json=QUS)

    with pytest.raises(ValueError, match=r"one of: \['Crate', 'Liter'\]"):
        await grocy_client.resolve_qu_for_product("Piece", PRODUCTS[0])

    assert units_route.calls.call_count == 1


async def test_product_specific_conversion_overrides_global(httpx2_mock: respx.Router) -> None:
    """When both global and product-specific conversions exist, product-specific wins."""
    conversions_with_global = [
        *CONVERSIONS,
        # Global Liter -> Kilogram with factor 1.0 (product-specific is 0.8)
        {"id": 3, "from_qu_id": 5, "to_qu_id": 3, "factor": 1.0, "product_id": None},
    ]

    router = httpx2_mock
    router.get("/objects/quantity_units").respond(json=QUS)
    router.get("/objects/quantity_unit_conversions_resolved").respond(json=conversions_with_global)

    async with GrocyClient(base_url=BASE_URL) as client:
        # Should use product-specific factor (0.8), not global (1.0)
        result = await client.resolve_qu_for_product("Liter", PRODUCTS[0])
    assert result.conversion_factor == 0.8


async def test_implicit_stock_unit_keeps_missing_unit_id(grocy_client: GrocyClient) -> None:
    product = {**PRODUCTS[0], "qu_id_stock": 99}
    assert await grocy_client.resolve_qu_for_product(None, product) == ResolvedQU(
        name="id=99", stock_qu_name=None, conversion_factor=1.0
    )


async def test_entity_operations_fetch_fresh_per_call(httpx2_mock: respx.Router) -> None:
    """Entity methods are stateless: every lookup re-fetches from Grocy.

    The MCP server isn't the only client of a Grocy instance, so we
    deliberately don't cache — a cache would hide state another client
    changed behind us. One `/objects/products` GET per resolve is the
    correct, intended cost.
    """
    router = httpx2_mock
    products_route = router.get("/objects/products").respond(json=PRODUCTS)
    router.get("/objects/quantity_units").respond(json=QUS)

    async with GrocyClient(base_url=BASE_URL) as client:
        await client.resolve_entity(EntityType.PRODUCT, "Rice")
        await client.resolve_entity(EntityType.PRODUCT, "Milk")
        await client.resolve_entity(EntityType.PRODUCT, 1)

    assert products_route.calls.call_count == 3


if __name__ == "__main__":
    pytest_bazel.main()
