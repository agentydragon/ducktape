"""Test structural equivalence between Zod hook schemas and Pydantic models.

The Zod schemas are extracted from the Claude Code binary and represent the
source of truth. This test converts them to JSON Schema via z.toJSONSchema()
(at Bazel build time) and compares against Pydantic's model_json_schema()
at test runtime.

Any delta is a test failure — there is no allowlist. Fix the Pydantic model
or explicitly acknowledge the delta.
"""

import difflib
import json
from typing import Any

import pytest
import pytest_bazel
from pydantic import TypeAdapter

from devinfra.claude.claude_api.hooks.output import HookOutput
from util.bazel.runfiles import get_required_path
from util.json_schema import inline_refs


def _load_zod_json_schema() -> dict[str, Any]:
    """Load the Zod-derived JSON Schema for hookOutput from runfiles."""
    path = get_required_path("_main/devinfra/claude/claude_api/hooks/schemas/2.1.105/hook_output.json")
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JSON Schema field for comparison.

    Strips metadata and structural differences that don't affect wire compatibility.
    """
    _strip_keys = {"title", "description", "default", "discriminator", "$defs", "$schema"}
    out = {k: v for k, v in schema.items() if k not in _strip_keys}
    if out.get("additionalProperties") in ({}, True):
        out["additionalProperties"] = True
        out.pop("propertyNames", None)
    if "oneOf" in out and "anyOf" not in out:
        out["anyOf"] = out.pop("oneOf")
    if "anyOf" in out:
        non_null = [v for v in out["anyOf"] if v != {"type": "null"}]
        if len(non_null) == 1 and len(out["anyOf"]) == len(non_null) + 1:
            collapsed = {k: v for k, v in out.items() if k != "anyOf"}
            inner = {k: v for k, v in non_null[0].items() if k not in _strip_keys}
            collapsed.update(inner)
            return _normalize(collapsed)
    if "required" in out and "properties" in out:
        props = out["properties"]
        out["required"] = [
            f for f in out["required"] if not (f in props and isinstance(props[f], dict) and "const" in props[f])
        ]
        if not out["required"]:
            del out["required"]
    if "anyOf" in out:
        out["anyOf"] = [_normalize(v) if isinstance(v, dict) else v for v in out["anyOf"]]
    if "properties" in out:
        out["properties"] = {k: _normalize(v) for k, v in out["properties"].items()}
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = _normalize(out["items"])
    return out


def _extract_variants(hso_schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract hookSpecificOutput variants keyed by hookEventName.

    Handles both flat (Zod: anyOf: [variant, ...]) and nested (Pydantic:
    anyOf: [{oneOf: [variant, ...]}, {type: null}]) structures.
    """
    variants: dict[str, dict[str, Any]] = {}
    for item in hso_schema.get("anyOf", hso_schema.get("oneOf", [])):
        if not isinstance(item, dict):
            continue
        hen = item.get("properties", {}).get("hookEventName", {})
        name = hen.get("const") or (hen.get("enum", [None])[0] if "enum" in hen else None)
        if name:
            variants[name] = item
        elif "oneOf" in item or "anyOf" in item:
            variants.update(_extract_variants(item))
    return variants


@pytest.fixture(scope="module")
def zod_schema() -> dict[str, Any]:
    return _load_zod_json_schema()


def test_zod_json_schema_loads(zod_schema: dict[str, Any]) -> None:
    props = zod_schema["properties"]
    assert "continue" in props
    assert "hookSpecificOutput" in props


def test_top_level_structure_matches_zod(zod_schema: dict[str, Any]) -> None:
    """Top-level properties and additionalProperties match between Zod and Pydantic."""
    pydantic_schema = inline_refs(TypeAdapter(HookOutput).json_schema(mode="serialization"))

    zod_props = set(zod_schema.get("properties", {}).keys())
    pydantic_props = set(pydantic_schema.get("properties", {}).keys())

    missing = zod_props - pydantic_props
    assert not missing, f"Zod top-level properties missing from Pydantic: {missing}"

    extra = pydantic_props - zod_props
    assert not extra, f"Pydantic has extra top-level properties not in Zod: {extra}"

    zod_ap = zod_schema.get("additionalProperties")
    pydantic_ap = pydantic_schema.get("additionalProperties")
    assert zod_ap == pydantic_ap, f"additionalProperties mismatch: Zod={zod_ap}, Pydantic={pydantic_ap}"


def test_common_fields_match(zod_schema: dict[str, Any]) -> None:
    """Common (non-hookSpecificOutput) fields match between Zod and Pydantic."""
    pydantic_schema = inline_refs(TypeAdapter(HookOutput).json_schema(mode="serialization"))

    common_fields = {"continue", "suppressOutput", "stopReason", "decision", "reason", "systemMessage"}
    for field in common_fields:
        zod_field = _normalize(zod_schema["properties"][field])
        pydantic_field = _normalize(pydantic_schema["properties"][field])
        if zod_field != pydantic_field:
            pytest.fail(
                f"Field {field!r} mismatch:\n"
                f"  Zod:     {json.dumps(zod_field, sort_keys=True)}\n"
                f"  Pydantic: {json.dumps(pydantic_field, sort_keys=True)}"
            )


def test_all_zod_variants_covered(zod_schema: dict[str, Any]) -> None:
    """Every Zod hookSpecificOutput variant has a Pydantic counterpart."""
    zod_variants = _extract_variants(zod_schema["properties"]["hookSpecificOutput"])
    pydantic_schema = inline_refs(TypeAdapter(HookOutput).json_schema(mode="serialization"))
    pydantic_variants = _extract_variants(pydantic_schema["properties"]["hookSpecificOutput"])

    missing = set(zod_variants) - set(pydantic_variants)
    assert not missing, f"Zod variants without Pydantic counterparts: {missing}"


@pytest.mark.parametrize(
    "event_name", sorted(_extract_variants(_load_zod_json_schema()["properties"]["hookSpecificOutput"]))
)
def test_variant_matches_zod(zod_schema: dict[str, Any], event_name: str) -> None:
    """Each hookSpecificOutput variant matches between Zod and Pydantic."""
    zod_variants = _extract_variants(zod_schema["properties"]["hookSpecificOutput"])
    pydantic_schema = inline_refs(TypeAdapter(HookOutput).json_schema(mode="serialization"))
    pydantic_variants = _extract_variants(pydantic_schema["properties"]["hookSpecificOutput"])

    if event_name not in pydantic_variants:
        pytest.fail(f"Pydantic missing variant for {event_name}")

    zod_normalized = _normalize(zod_variants[event_name])
    pydantic_normalized = _normalize(pydantic_variants[event_name])

    if zod_normalized != pydantic_normalized:
        zod_text = json.dumps(zod_normalized, indent=2, sort_keys=True)
        pyd_text = json.dumps(pydantic_normalized, indent=2, sort_keys=True)
        diff = "\n".join(
            difflib.unified_diff(
                zod_text.splitlines(),
                pyd_text.splitlines(),
                fromfile=f"Zod ({event_name})",
                tofile=f"Pydantic ({event_name})",
                lineterm="",
                n=3,
            )
        )
        pytest.fail(f"Schema mismatch for {event_name}:\n{diff}")


if __name__ == "__main__":
    pytest_bazel.main()
