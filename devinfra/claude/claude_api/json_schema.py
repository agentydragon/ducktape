"""JSON Schema utilities for structural comparison and ref inlining."""

from typing import Any

_DEFS_REF_PREFIX = "#/$defs/"


def inline_refs(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inline references to local `$defs` for structural comparison.

    If `defs` is None, uses schema["$defs"] (top-level call). For nested calls,
    pass the same defs dict through. A missing local definition raises ValueError.
    """
    if defs is None:
        defs = schema.get("$defs", {})
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith(_DEFS_REF_PREFIX):
            def_name = ref[len(_DEFS_REF_PREFIX) :]
            if def_name not in defs:
                raise ValueError(f"Missing JSON Schema definition for {ref!r}")
            # Merge sibling keys (e.g. default) with the resolved definition
            return {**inline_refs(defs[def_name], defs), **{k: v for k, v in schema.items() if k != "$ref"}}
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
