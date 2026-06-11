"""Tests for the build-time OpenAPI spec fixups."""

from __future__ import annotations

import json

import pytest_bazel

from util.bazel.runfiles import get_required_path
from x.grocy_mcp.fix_openapi_spec import (
    _MISSING_PRODUCT_PROPERTIES,
    fix_entity_refs,
    patch_product_schema,
    strip_empty_enums,
    strip_replaced_routes,
)
from x.grocy_mcp.grocy_types import PRODUCT_WRITABLE_FIELDS


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


def test_fixed_spec_has_every_writable_product_column() -> None:
    """The built spec served at runtime must have every writable column of the products table.

    Catches the regression where someone adds a field to
    ``PRODUCT_WRITABLE_FIELDS`` without also adding its schema entry to
    ``_MISSING_PRODUCT_PROPERTIES`` (or vice versa, once Grocy upstream
    starts shipping a complete spec). This is the smoke test the
    ``entity_update`` feedback asked for: every field named in the tool's
    docstring examples must actually be accepted by the tool's input schema.
    """
    spec_path = get_required_path("_main/x/grocy_mcp/grocy.openapi.fixed.json")
    spec = json.loads(spec_path.read_text())
    properties = spec["components"]["schemas"]["Product"]["properties"]

    missing = PRODUCT_WRITABLE_FIELDS - properties.keys()
    assert not missing, (
        f"PRODUCT_WRITABLE_FIELDS entries missing from patched Product schema: {sorted(missing)}. "
        f"Add them to _MISSING_PRODUCT_PROPERTIES in fix_openapi_spec.py."
    )


def test_patch_product_schema_does_not_clobber_existing_upstream_property() -> None:
    """If Grocy ever adds a patched field upstream, the upstream schema wins."""
    sample_field = next(iter(_MISSING_PRODUCT_PROPERTIES))
    upstream_definition = {"type": "integer", "description": "upstream wins"}
    spec: dict = {
        "components": {"schemas": {"Product": {"type": "object", "properties": {sample_field: upstream_definition}}}}
    }
    patch_product_schema(spec)
    assert spec["components"]["schemas"]["Product"]["properties"][sample_field] == upstream_definition


if __name__ == "__main__":
    pytest_bazel.main()
