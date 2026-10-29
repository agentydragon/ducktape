"""JSON Schema utilities: ref inlining for structural comparison, and registering Pydantic models
into an existing definition table (FastAPI's `components.schemas`, a `$defs` map)."""

from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter
from pydantic.json_schema import JsonSchemaMode

_DEFS_REF_PREFIX = "#/$defs/"


def inline_refs(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recursively inline all `$ref` references so schemas can be compared structurally.

    If `defs` is None, uses schema["$defs"] (top-level call). For nested calls,
    pass the same defs dict through.
    """
    if defs is None:
        defs = schema.get("$defs", {})
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith(_DEFS_REF_PREFIX):
            def_name = ref[len(_DEFS_REF_PREFIX) :]
            resolved = defs.get(def_name, schema)
            # Merge sibling keys (e.g. default) with the resolved definition
            return {**inline_refs(resolved, defs), **{k: v for k, v in schema.items() if k != "$ref"}}
        return schema
    result: dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            result[k] = inline_refs(v, defs)
        elif isinstance(v, list):
            result[k] = [inline_refs(item, defs) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


class SchemaNameConflictError(ValueError):
    """A definition name to register is already taken by a different schema."""

    def __init__(self, name: str) -> None:
        super().__init__(f"schema definition {name=} already exists with a different body")
        self.name = name


def register_models(
    definitions: dict[str, Any], entries: Sequence[tuple[str, JsonSchemaMode, TypeAdapter[Any]]], *, ref_template: str
) -> None:
    """Add each entry's schema to `definitions` under its key, plus the nested definitions it needs.

    All entries go through one `TypeAdapter.json_schemas` call, so Pydantic itself gives distinct
    same-named nested models distinct names and emits a shared nested model once. `ref_template`
    must address `definitions` (`"#/components/schemas/{model}"` for an OpenAPI document).

    Pydantic cannot see what `definitions` already holds, so every name is checked before insertion:
    an identical body is skipped, a different body raises `SchemaNameConflictError`. A model root comes
    back as a `$ref` to its own definition; when that definition's name is the entry's key, the
    body already sits under the key and the `$ref` is not inserted over it.
    """
    roots, allocated = TypeAdapter.json_schemas(entries, ref_template=ref_template)
    for name, body in allocated.get("$defs", {}).items():
        _register(definitions, name, body)
    for (key, _mode), root in roots.items():
        if root == {"$ref": ref_template.format(model=key)}:
            continue
        _register(definitions, key, root)


def _register(definitions: dict[str, Any], name: str, body: dict[str, Any]) -> None:
    if name not in definitions:
        definitions[name] = body
    elif definitions[name] != body:
        raise SchemaNameConflictError(name)
