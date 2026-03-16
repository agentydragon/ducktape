"""Test structural equivalence between Zod hook schemas and Pydantic models.

The Zod schemas are extracted from the Claude Code binary and represent the
source of truth. This test converts them to JSON Schema via z.toJSONSchema()
(at Bazel build time) and compares against Pydantic's model_json_schema()
at test runtime.

Any delta is a test failure — there is no allowlist. Fix the Pydantic model
or explicitly acknowledge the delta.
"""

import json
from typing import Any

import pytest_bazel
from pydantic import TypeAdapter

from devinfra.claude.claude_api.hooks.config_change import ConfigChangeOutput
from devinfra.claude.claude_api.hooks.elicitation import ElicitationOutput, ElicitationResultOutput
from devinfra.claude.claude_api.hooks.notification import NotificationOutput
from devinfra.claude.claude_api.hooks.permission_request import PermissionRequestOutput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseOutput
from devinfra.claude.claude_api.hooks.post_tool_use_failure import PostToolUseFailureOutput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseOutput
from devinfra.claude.claude_api.hooks.session_start import SessionStartOutput
from devinfra.claude.claude_api.hooks.setup import SetupOutput
from devinfra.claude.claude_api.hooks.stop import StopOutput, SubagentStopOutput
from devinfra.claude.claude_api.hooks.subagent_start import SubagentStartOutput
from devinfra.claude.claude_api.hooks.user_prompt_submit import UserPromptSubmitOutput
from util.bazel.runfiles import get_required_path


def _load_zod_json_schema() -> dict[str, Any]:
    """Load the Zod-derived JSON Schema for hookOutput from runfiles."""
    path = get_required_path("_main/devinfra/claude/claude_api/hooks/schemas/2.1.76/hook_output.json")
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def _pydantic_json_schema(model_class: type) -> dict[str, Any]:
    """Export a Pydantic model as JSON Schema (serialization mode, camelCase aliases)."""
    return TypeAdapter(model_class).json_schema(mode="serialization")


def _extract_zod_common_fields(zod_schema: dict[str, Any]) -> dict[str, Any]:
    """Extract the common (non-hookSpecificOutput) field schemas from the Zod hookOutput."""
    props = zod_schema.get("properties", {})
    return {k: v for k, v in props.items() if k != "hookSpecificOutput"}


def _extract_zod_hook_specific_variants(zod_schema: dict[str, Any]) -> dict[str, Any]:
    """Extract hookSpecificOutput union variants, keyed by hookEventName literal."""
    hso = zod_schema.get("properties", {}).get("hookSpecificOutput", {})
    # The hookSpecificOutput is optional, so it may be wrapped in anyOf with the union
    variants = {}
    # Try direct anyOf (union members)
    any_of = hso.get("anyOf", [])
    if not any_of:
        # Could be a oneOf
        any_of = hso.get("oneOf", [])
    for variant in any_of:
        props = variant.get("properties", {})
        hen = props.get("hookEventName", {})
        # Literal value is stored as const or enum
        name = hen.get("const") or (hen.get("enum", [None])[0] if "enum" in hen else None)
        if name:
            variants[name] = variant
    return variants


# Map from Zod hookEventName to Pydantic output model class
_HOOK_OUTPUT_MODELS: dict[str, type] = {
    "SessionStart": SessionStartOutput,
    "Setup": SetupOutput,
    "PreToolUse": PreToolUseOutput,
    "PostToolUse": PostToolUseOutput,
    "PostToolUseFailure": PostToolUseFailureOutput,
    "UserPromptSubmit": UserPromptSubmitOutput,
    "Notification": NotificationOutput,
    "PermissionRequest": PermissionRequestOutput,
    "Elicitation": ElicitationOutput,
    "ElicitationResult": ElicitationResultOutput,
    "SubagentStart": SubagentStartOutput,
}

# Models that share the same common fields but have no hookSpecificOutput in Zod
_COMMON_ONLY_MODELS: dict[str, type] = {
    "Stop": StopOutput,
    "SubagentStop": SubagentStopOutput,
    "ConfigChange": ConfigChangeOutput,
}


def test_zod_json_schema_loads() -> None:
    """Smoke test: the generated JSON Schema file loads and has expected structure."""
    schema = _load_zod_json_schema()
    assert "properties" in schema, f"Expected 'properties' in schema, got keys: {list(schema.keys())}"
    props = schema["properties"]
    assert "continue" in props, f"Expected 'continue' field, got: {list(props.keys())}"
    assert "hookSpecificOutput" in props, f"Expected 'hookSpecificOutput' field, got: {list(props.keys())}"


def test_common_fields_match() -> None:
    """Verify that common fields (continue, suppressOutput, etc.) in the Zod
    hookOutput schema are present with compatible types in each Pydantic output model."""
    zod_schema = _load_zod_json_schema()
    zod_common = _extract_zod_common_fields(zod_schema)
    zod_required = set(zod_schema.get("required", []))

    all_models = {**_HOOK_OUTPUT_MODELS, **_COMMON_ONLY_MODELS}
    deltas: list[str] = []

    for model_name, model_class in sorted(all_models.items()):
        pydantic_schema = _pydantic_json_schema(model_class)
        pydantic_props = pydantic_schema.get("properties", {})
        pydantic_required = set(pydantic_schema.get("required", []))

        for field_name, zod_field in zod_common.items():
            if field_name not in pydantic_props:
                deltas.append(f"[{model_name}] Missing field '{field_name}' in Pydantic model {model_class.__name__}")
                continue

            pydantic_field = pydantic_props[field_name]
            # Compare type representations (excluding title/description metadata)
            if _normalize_field_schema(zod_field) != _normalize_field_schema(pydantic_field):
                deltas.append(
                    f"[{model_name}] Field '{field_name}' differs:\n"
                    f"  Zod:     {json.dumps(zod_field, sort_keys=True)}\n"
                    f"  Pydantic: {json.dumps(pydantic_field, sort_keys=True)}"
                )

            # Compare required status
            zod_req = field_name in zod_required
            pyd_req = field_name in pydantic_required
            if zod_req != pyd_req:
                deltas.append(
                    f"[{model_name}] Field '{field_name}' required mismatch: "
                    f"Zod={'required' if zod_req else 'optional'}, "
                    f"Pydantic={'required' if pyd_req else 'optional'}"
                )

        # Check for extra fields in Pydantic that aren't in Zod common fields
        # (excluding hookSpecificOutput which is handled separately)
        for field_name in pydantic_props:
            if field_name not in zod_common and field_name != "hookSpecificOutput":
                deltas.append(
                    f"[{model_name}] Extra field '{field_name}' in Pydantic model "
                    f"{model_class.__name__} not in Zod common fields"
                )

    if deltas:
        raise AssertionError(f"Schema equivalence check found {len(deltas)} delta(s):\n\n" + "\n\n".join(deltas))


def test_hook_specific_output_variants_match() -> None:
    """Verify that each hookSpecificOutput variant in Zod has a matching
    Pydantic model with compatible fields."""
    zod_schema = _load_zod_json_schema()
    zod_variants = _extract_zod_hook_specific_variants(zod_schema)

    deltas: list[str] = []

    # Check all Zod variants have a corresponding Pydantic model
    for event_name in sorted(zod_variants.keys()):
        if event_name not in _HOOK_OUTPUT_MODELS:
            deltas.append(
                f"Zod hookSpecificOutput has variant '{event_name}' with no corresponding Pydantic output model"
            )

    # Compare each variant's fields
    for event_name, model_class in sorted(_HOOK_OUTPUT_MODELS.items()):
        if event_name not in zod_variants:
            deltas.append(
                f"Pydantic has output model for '{event_name}' but Zod hookSpecificOutput has no matching variant"
            )
            continue

        zod_variant = zod_variants[event_name]
        zod_props = zod_variant.get("properties", {})

        # Get the Pydantic hookSpecificOutput type's schema
        pydantic_schema = _pydantic_json_schema(model_class)
        pydantic_props = pydantic_schema.get("properties", {})
        hso_field = pydantic_props.get("hookSpecificOutput", {})

        # Resolve the hookSpecificOutput type — it may be a $ref or inline
        pydantic_full = _pydantic_json_schema(model_class)
        defs = pydantic_full.get("$defs", {})

        # Get the actual hookSpecificOutput schema
        hso_schema = _resolve_ref(hso_field, defs)

        if not hso_schema:
            deltas.append(
                f"[{event_name}] Could not resolve hookSpecificOutput schema from Pydantic model {model_class.__name__}"
            )
            continue

        hso_props = hso_schema.get("properties", {})

        # Compare fields
        for field_name, zod_field_schema in zod_props.items():
            if field_name not in hso_props:
                deltas.append(
                    f"[{event_name}] Missing field '{field_name}' in Pydantic "
                    f"hookSpecificOutput for {model_class.__name__}"
                )
                continue

            pydantic_field_schema = _inline_refs(hso_props[field_name], defs)
            if _normalize_field_schema(zod_field_schema) != _normalize_field_schema(pydantic_field_schema):
                deltas.append(
                    f"[{event_name}] hookSpecificOutput field '{field_name}' differs:\n"
                    f"  Zod:     {json.dumps(zod_field_schema, sort_keys=True)}\n"
                    f"  Pydantic: {json.dumps(pydantic_field_schema, sort_keys=True)}"
                )

        # Check for extra Pydantic fields not in Zod
        for field_name in hso_props:
            if field_name not in zod_props:
                deltas.append(
                    f"[{event_name}] Extra field '{field_name}' in Pydantic hookSpecificOutput not in Zod variant"
                )

    if deltas:
        raise AssertionError(
            f"hookSpecificOutput equivalence check found {len(deltas)} delta(s):\n\n" + "\n\n".join(deltas)
        )


def _normalize_field_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JSON Schema field for comparison.

    Strips metadata (title, description, default) and collapses Pydantic's
    nullable-optional pattern to match Zod's .optional() representation.

    Zod .optional() emits {"type": "string"} and omits the field from required.
    Pydantic `str | None = None` emits {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}.
    These are equivalent on the wire because we serialize with exclude_none=True,
    so None values are omitted (absent) rather than sent as null.
    """
    out = {k: v for k, v in schema.items() if k not in ("title", "description", "default")}
    # Zod emits {"additionalProperties": {}, "propertyNames": {"type": "string"}} for
    # Record<string, unknown>; Pydantic emits {"additionalProperties": true}. These are
    # semantically equivalent in JSON Schema (both mean "any additional string-keyed properties").
    if out.get("additionalProperties") in ({}, True):
        out["additionalProperties"] = True
        out.pop("propertyNames", None)
    # Collapse anyOf with a null variant: {"anyOf": [<real_type>, {"type": "null"}]}
    # becomes just <real_type>, matching Zod's .optional() representation.
    if "anyOf" in out:
        non_null = [v for v in out["anyOf"] if v != {"type": "null"}]
        if len(non_null) == 1 and len(out["anyOf"]) == len(non_null) + 1:
            # Replace the anyOf with the single non-null variant, keeping other keys.
            # Also strip metadata from the resolved variant (e.g. from inlined $refs).
            collapsed = {k: v for k, v in out.items() if k != "anyOf"}
            inner = {k: v for k, v in non_null[0].items() if k not in ("title", "description")}
            collapsed.update(inner)
            return collapsed
    return out


def _inline_refs(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Recursively inline all $ref references so schemas can be compared structurally."""
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            def_name = ref[len("#/$defs/") :]
            resolved = defs.get(def_name, schema)
            # Merge any sibling keys (e.g. default) with the resolved definition
            merged = {**_inline_refs(resolved, defs)}
            for k, v in schema.items():
                if k != "$ref":
                    merged[k] = v
            return merged
        return schema
    result: dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            result[k] = _inline_refs(v, defs)
        elif isinstance(v, list):
            result[k] = [_inline_refs(item, defs) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


def _resolve_ref(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a JSON Schema $ref to its definition."""
    if "$ref" in schema:
        ref = schema["$ref"]
        # Format: #/$defs/ClassName
        if ref.startswith("#/$defs/"):
            def_name = ref[len("#/$defs/") :]
            return defs.get(def_name)
    # Could be anyOf with a $ref (for optional types)
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            if "$ref" in variant:
                return _resolve_ref(variant, defs)
    return schema if "properties" in schema else None


if __name__ == "__main__":
    pytest_bazel.main()
