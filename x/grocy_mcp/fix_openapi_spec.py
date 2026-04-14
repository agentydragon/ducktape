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

This script reads the raw spec, applies both fixups, and writes the
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


def fix_created_object_id(spec: dict[str, object]) -> None:
    """Widen created_object_id to accept string or integer.

    Grocy returns created_object_id as a string despite the spec declaring it
    as integer, causing FastMCP output validation to fail.
    """
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return
    entity_route = paths.get("/objects/{entity}")
    if not isinstance(entity_route, dict):
        return
    post_op = entity_route.get("post")
    if not isinstance(post_op, dict):
        return
    try:
        props = post_op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    except (KeyError, TypeError):
        return
    if not isinstance(props, dict):
        return
    field = props.get("created_object_id")
    if isinstance(field, dict) and field.get("type") == "integer":
        props["created_object_id"] = {
            "anyOf": [{"type": "integer"}, {"type": "string"}],
            "description": field.get("description", "The id of the created object"),
        }


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.json> <output.json>", file=sys.stderr)
        sys.exit(1)

    spec: dict[str, object] = json.loads(Path(sys.argv[1]).read_text())
    strip_empty_enums(spec)
    fix_entity_refs(spec)
    fix_created_object_id(spec)
    Path(sys.argv[2]).write_text(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
