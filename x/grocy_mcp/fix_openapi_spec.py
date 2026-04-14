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
    strip_replaced_routes(spec)
    Path(sys.argv[2]).write_text(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
