"""Build-time fixups for Grocy's OpenAPI 3.1 spec.

Grocy's published spec has two classes of issues that prevent FastMCP's
``from_openapi`` from producing usable tools:

1. **Empty enums**: ``ExposedEntityEditRequiresAdmin`` has ``"enum": []``,
   which is invalid per OpenAPI and causes pydantic to reject the spec.

2. **Dangling ``$ref``s**: Path parameters reference computed schema names
   like ``ExposedEntity_NotIncludingNotEditable`` that Grocy generates at
   runtime but never includes in the static spec file. FastMCP silently
   drops parameters it can't resolve, making ``/objects/{entity}`` tools
   unusable (no ``entity`` parameter).

3. **Incomplete entity schemas**: Grocy's ``grocy.openapi.json`` is
   hand-maintained and has drifted from the DB schema — writable columns
   added by later migrations (e.g. ``qu_id_consume``, ``qu_id_price``,
   ``default_best_before_days_after_freezing``) never made it into
   ``components.schemas.Product.properties``. Without a patch these
   columns are invisible in the ``entity_update`` tool schema even though
   Grocy accepts them. TODO: file an upstream PR against
   https://github.com/grocy/grocy adding the missing Product properties
   (and similar gaps in Location, QuantityUnit, ShoppingListItem; and
   the entirely missing ShoppingList, ProductGroup, Recipe schemas).
   Until that lands and we pin a Grocy release including it, patch here.

Additionally, routes replaced by batch tools (``batch_tools.py``) are
stripped from the spec entirely so FastMCP never generates competing
single-item tool stubs for them.

This script reads the raw spec, applies all fixups, and writes the
corrected spec. It runs as a Bazel genrule so the server loads a
pre-fixed spec at runtime with no patching logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def strip_empty_enums(node: object) -> None:
    """Drop empty ``enum: []`` keys recursively."""
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list) and not node["enum"]:
            del node["enum"]
        for value in node.values():
            strip_empty_enums(value)
    elif isinstance(node, list):
        for item in node:
            strip_empty_enums(item)


def fix_entity_refs(spec: dict[str, object]) -> None:
    """Materialize derived ExposedEntity_* schemas Grocy references but never defines."""
    components = spec.get("components")
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    base_enum = schemas.get("ExposedEntity", {})
    base = set(base_enum.get("enum", [])) if isinstance(base_enum, dict) else set()

    simple = {
        "ExposedEntity_NotIncludingNotEditable": "ExposedEntityNoEdit",
        "ExposedEntity_NotIncludingNotDeletable": "ExposedEntityNoDelete",
        "ExposedEntity_NotIncludingNotListable": "ExposedEntityNoListing",
    }
    for derived_name, exclude_key in simple.items():
        if derived_name not in schemas:
            exclude_enum = schemas.get(exclude_key, {})
            exclude = set(exclude_enum.get("enum", [])) if isinstance(exclude_enum, dict) else set()
            schemas[derived_name] = {"type": "string", "enum": sorted(base - exclude)}

    if "ExposedEntity_IncludingUserEntities" not in schemas:
        schemas["ExposedEntity_IncludingUserEntities"] = {"type": "string", "enum": sorted(base)}
    if "ExposedEntity_IncludingUserEntities_NotIncludingNotEditable" not in schemas:
        exclude_enum = schemas.get("ExposedEntityNoEdit", {})
        exclude = set(exclude_enum.get("enum", [])) if isinstance(exclude_enum, dict) else set()
        schemas["ExposedEntity_IncludingUserEntities_NotIncludingNotEditable"] = {
            "type": "string",
            "enum": sorted(base - exclude),
        }


# Writable product columns missing from Grocy's hand-maintained spec.
# Types follow Grocy's existing style: booleans are encoded as ``integer``
# (0/1 in SQLite), FK IDs are ``integer``, amounts are ``number``. Keep in
# sync with ``PRODUCT_WRITABLE_FIELDS`` in grocy_types.py; the
# test_fix_openapi_spec smoke test enforces that every writable field is
# present in the patched schema.
_MISSING_PRODUCT_PROPERTIES: dict[str, dict[str, object]] = {
    "active": {"type": "integer", "default": 1, "description": "0 or 1 (boolean)."},
    "calories": {"type": "number", "description": "Calories per stock QU."},
    "cumulate_min_stock_amount_of_sub_products": {
        "type": "integer",
        "default": 0,
        "description": "0 or 1 (boolean). Sum child product stock toward this parent's minimum.",
    },
    "default_best_before_days_after_freezing": {
        "type": "integer",
        "minimum": -1,
        "default": 0,
        "description": "-1 = never expires after freezing.",
    },
    "default_best_before_days_after_thawing": {"type": "integer", "minimum": 0, "default": 0},
    "default_stock_label_type": {"type": "integer", "default": 0},
    "due_type": {"type": "integer", "default": 1, "description": "1 = best before, 2 = expiration."},
    "hide_on_stock_overview": {"type": "integer", "default": 0, "description": "0 or 1 (boolean)."},
    "parent_product_id": {"type": "integer", "description": "FK to products.id; omit for top-level."},
    "qu_id_consume": {"type": "integer", "description": "Default QU for consume (FK to quantity_units)."},
    "qu_id_price": {"type": "integer", "description": "Default QU for price display (FK to quantity_units)."},
    "quick_consume_amount": {"type": "number", "default": 1},
}


def patch_product_schema(spec: dict[str, object]) -> None:
    """Add missing writable Product columns to the spec.

    Raises KeyError if the spec no longer contains ``components.schemas.Product``
    — that would mean Grocy restructured the spec and this patch needs revisiting.
    """
    schemas = spec["components"]["schemas"]  # type: ignore[index]
    properties = schemas["Product"]["properties"]
    for name, definition in _MISSING_PRODUCT_PROPERTIES.items():
        if name not in properties:
            properties[name] = dict(definition)


# Routes replaced by custom batch tools in batch_tools.py.
# Stripped from the spec so FastMCP never generates competing stubs for them.
_REPLACED_ROUTES: set[tuple[str, str]] = {
    ("get", "/objects/{entity}"),
    ("post", "/objects/{entity}"),
    ("get", "/objects/{entity}/{objectId}"),
    ("get", "/stock"),
    ("post", "/stock/products/{productId}/add"),
    ("post", "/stock/products/{productId}/consume"),
    ("post", "/stock/products/{productId}/inventory"),
    ("post", "/stock/products/{productId}/transfer"),
    ("get", "/stock/entry/{entryId}"),
    ("put", "/stock/entry/{entryId}"),
    ("post", "/stock/shoppinglist/add-product"),
    ("post", "/stock/shoppinglist/remove-product"),
    ("post", "/stock/shoppinglist/clear"),
}


def strip_replaced_routes(spec: dict[str, object]) -> None:
    """Remove routes replaced by batch_tools.py from the spec.

    Keeps the path entry if other HTTP methods remain; removes the path
    entirely if all its methods are stripped.
    """
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return
    empty_paths = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in list(path_item):
            if (method, path) in _REPLACED_ROUTES:
                del path_item[method]
        if not any(m in path_item for m in ("get", "post", "put", "patch", "delete")):
            empty_paths.append(path)
    for path in empty_paths:
        del paths[path]


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)

    spec: dict[str, object] = json.loads(Path(sys.argv[1]).read_text())
    strip_empty_enums(spec)
    fix_entity_refs(spec)
    patch_product_schema(spec)
    strip_replaced_routes(spec)
    Path(sys.argv[2]).write_text(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
