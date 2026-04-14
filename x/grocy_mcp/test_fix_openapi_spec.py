"""Tests for the build-time OpenAPI spec fixups."""

from __future__ import annotations

import pytest_bazel

from x.grocy_mcp.fix_openapi_spec import fix_entity_refs, strip_empty_enums, strip_replaced_routes


def test_strip_empty_enums_removes_empty_enum() -> None:
    spec = {
        "components": {"schemas": {"Bad": {"type": "string", "enum": []}, "Good": {"type": "string", "enum": ["ok"]}}}
    }
    strip_empty_enums(spec)
    assert "enum" not in spec["components"]["schemas"]["Bad"]
    assert spec["components"]["schemas"]["Good"]["enum"] == ["ok"]


def test_fix_entity_refs_materializes_missing_schemas() -> None:
    spec: dict = {
        "components": {
            "schemas": {
                "ExposedEntity": {"type": "string", "enum": ["products", "locations", "stock_log"]},
                "ExposedEntityNoEdit": {"type": "string", "enum": ["stock_log"]},
                "ExposedEntityNoDelete": {"type": "string", "enum": ["stock_log"]},
                "ExposedEntityNoListing": {"type": "string", "enum": []},
            }
        }
    }
    fix_entity_refs(spec)
    schemas = spec["components"]["schemas"]

    assert schemas["ExposedEntity_NotIncludingNotEditable"]["enum"] == ["locations", "products"]
    assert schemas["ExposedEntity_NotIncludingNotDeletable"]["enum"] == ["locations", "products"]
    assert schemas["ExposedEntity_NotIncludingNotListable"]["enum"] == ["locations", "products", "stock_log"]
    assert schemas["ExposedEntity_IncludingUserEntities"]["enum"] == ["locations", "products", "stock_log"]
    assert schemas["ExposedEntity_IncludingUserEntities_NotIncludingNotEditable"]["enum"] == ["locations", "products"]


def test_strip_replaced_routes_removes_replaced_methods() -> None:
    spec: dict = {
        "paths": {
            "/objects/{entity}": {"get": {"summary": "list"}, "post": {"summary": "create"}},
            "/objects/{entity}/{objectId}": {
                "get": {"summary": "get one"},
                "put": {"summary": "update"},
                "delete": {"summary": "delete"},
            },
            "/stock": {"get": {"summary": "stock"}},
        }
    }
    strip_replaced_routes(spec)
    paths = spec["paths"]

    # GET and POST on /objects/{entity} both replaced — path should be gone entirely.
    assert "/objects/{entity}" not in paths

    # Only GET replaced on /objects/{entity}/{objectId} — PUT and DELETE remain.
    assert "/objects/{entity}/{objectId}" in paths
    assert "get" not in paths["/objects/{entity}/{objectId}"]
    assert "put" in paths["/objects/{entity}/{objectId}"]
    assert "delete" in paths["/objects/{entity}/{objectId}"]

    # /stock GET replaced — path gone.
    assert "/stock" not in paths


if __name__ == "__main__":
    pytest_bazel.main()
