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
from devinfra.claude.claude_api.hooks.stop import StopOutput
from devinfra.claude.claude_api.hooks.subagent_start import SubagentStartOutput
from devinfra.claude.claude_api.hooks.subagent_stop import SubagentStopOutput
from devinfra.claude.claude_api.hooks.user_prompt_submit import UserPromptSubmitOutput
from util.bazel.runfiles import get_required_path
from util.json_schema import inline_refs


def _load_zod_json_schema() -> dict[str, Any]:
    """Load the Zod-derived JSON Schema for hookOutput from runfiles."""
    path = get_required_path("_main/devinfra/claude/claude_api/hooks/schemas/2.1.76/hook_output.json")
    return json.loads(path.read_text())  # type: ignore[no-any-return]


# Map from Zod hookEventName to Pydantic output model class.
# Models with hookSpecificOutput in Zod:
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

# Models that share the same common fields but have no hookSpecificOutput in Zod:
_COMMON_ONLY_MODELS: dict[str, type] = {
    "Stop": StopOutput,
    "SubagentStop": SubagentStopOutput,
    "ConfigChange": ConfigChangeOutput,
}


def _compose_zod_model_schema(zod_schema: dict[str, Any], event_name: str | None) -> dict[str, Any]:
    """Compose the expected Zod schema for a specific hook output model.

    Combines the common hookOutput fields with the hookSpecificOutput variant
    matching event_name (if any), producing a schema comparable to a Pydantic
    model's JSON schema.
    """
    zod_props = dict(zod_schema.get("properties", {}))
    zod_required = list(zod_schema.get("required", []))

    if event_name is None:
        # Common-only model: no hookSpecificOutput
        zod_props.pop("hookSpecificOutput", None)
        zod_required = [r for r in zod_required if r != "hookSpecificOutput"]
    else:
        # Find the matching variant in the hookSpecificOutput union
        hso = zod_props.get("hookSpecificOutput", {})
        variants = hso.get("anyOf", hso.get("oneOf", []))
        matched = None
        for variant in variants:
            props = variant.get("properties", {})
            hen = props.get("hookEventName", {})
            name = hen.get("const") or (hen.get("enum", [None])[0] if "enum" in hen else None)
            if name == event_name:
                matched = variant
                break
        if matched is not None:
            # Replace the union with the specific variant (wrapped as optional)
            zod_props["hookSpecificOutput"] = matched

    result: dict[str, Any] = {"type": "object", "properties": zod_props}
    if zod_required:
        result["required"] = zod_required
    if "additionalProperties" in zod_schema:
        result["additionalProperties"] = zod_schema["additionalProperties"]
    return result


def _compare_schemas(zod_schema: dict[str, Any], pydantic_schema: dict[str, Any], label: str) -> list[str]:
    """Compare two object schemas property-by-property. Returns list of delta descriptions."""
    deltas: list[str] = []
    zod_props = zod_schema.get("properties", {})
    zod_required = set(zod_schema.get("required", []))

    pydantic_full = inline_refs(pydantic_schema)
    pydantic_props = pydantic_full.get("properties", {})
    pydantic_required = set(pydantic_full.get("required", []))

    for field_name, zod_field in zod_props.items():
        if field_name not in pydantic_props:
            deltas.append(f"[{label}] Missing field '{field_name}' in Pydantic model")
            continue

        pydantic_field = pydantic_props[field_name]
        if _normalize(zod_field) != _normalize(pydantic_field):
            deltas.append(
                f"[{label}] Field '{field_name}' differs:\n"
                f"  Zod:     {json.dumps(zod_field, sort_keys=True)}\n"
                f"  Pydantic: {json.dumps(pydantic_field, sort_keys=True)}"
            )

        zod_req = field_name in zod_required
        pyd_req = field_name in pydantic_required
        if zod_req != pyd_req:
            deltas.append(
                f"[{label}] Field '{field_name}' required mismatch: "
                f"Zod={'required' if zod_req else 'optional'}, "
                f"Pydantic={'required' if pyd_req else 'optional'}"
            )

    for field_name in pydantic_props:
        if field_name not in zod_props:
            deltas.append(f"[{label}] Extra field '{field_name}' in Pydantic model not in Zod")

    return deltas


def test_zod_json_schema_loads() -> None:
    """Smoke test: the generated JSON Schema file loads and has expected structure."""
    schema = _load_zod_json_schema()
    assert "properties" in schema, f"Expected 'properties' in schema, got keys: {list(schema.keys())}"
    props = schema["properties"]
    assert "continue" in props, f"Expected 'continue' field, got: {list(props.keys())}"
    assert "hookSpecificOutput" in props, f"Expected 'hookSpecificOutput' field, got: {list(props.keys())}"


def test_output_models_match_zod() -> None:
    """Verify each Pydantic output model matches the Zod hookOutput schema."""
    zod_schema = _load_zod_json_schema()
    deltas: list[str] = []

    # Models with hookSpecificOutput variants
    for event_name, model_class in sorted(_HOOK_OUTPUT_MODELS.items()):
        zod_composed = _compose_zod_model_schema(zod_schema, event_name)
        pydantic_schema = TypeAdapter(model_class).json_schema(mode="serialization")
        deltas.extend(_compare_schemas(zod_composed, pydantic_schema, event_name))

    # Common-only models (no hookSpecificOutput)
    for event_name, model_class in sorted(_COMMON_ONLY_MODELS.items()):
        zod_composed = _compose_zod_model_schema(zod_schema, None)
        pydantic_schema = TypeAdapter(model_class).json_schema(mode="serialization")
        deltas.extend(_compare_schemas(zod_composed, pydantic_schema, event_name))

    if deltas:
        raise AssertionError(f"Schema equivalence check found {len(deltas)} delta(s):\n\n" + "\n\n".join(deltas))


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JSON Schema field for comparison.

    Strips metadata and structural differences that don't affect wire compatibility:
    - Strip metadata keys: title, description, default, discriminator
    - Collapse nullable anyOf: {"anyOf": [T, {"type": "null"}]} → T
    - Normalize Record<string, unknown>: additionalProperties:{} ≡ true
    - Unify oneOf → anyOf (Pydantic uses oneOf for discriminated unions, Zod uses anyOf)
    - Normalize required: remove const-defaulted fields (always have a fixed value)
    - Recurse into nested structures (properties, items, anyOf/oneOf variants)
    """
    _strip_keys = {"title", "description", "default", "discriminator"}
    out = {k: v for k, v in schema.items() if k not in _strip_keys}
    # Zod emits {"additionalProperties": {}, "propertyNames": {"type": "string"}} for
    # Record<string, unknown>; Pydantic emits {"additionalProperties": true}. These are
    # semantically equivalent in JSON Schema (both mean "any additional string-keyed properties").
    if out.get("additionalProperties") in ({}, True):
        out["additionalProperties"] = True
        out.pop("propertyNames", None)
    # Pydantic uses oneOf for discriminated unions, Zod uses anyOf. Normalize to anyOf.
    if "oneOf" in out and "anyOf" not in out:
        out["anyOf"] = out.pop("oneOf")
    # Collapse anyOf with a null variant: {"anyOf": [<real_type>, {"type": "null"}]}
    # becomes just <real_type>, matching Zod's .optional() representation.
    if "anyOf" in out:
        non_null = [v for v in out["anyOf"] if v != {"type": "null"}]
        if len(non_null) == 1 and len(out["anyOf"]) == len(non_null) + 1:
            collapsed = {k: v for k, v in out.items() if k != "anyOf"}
            inner = {k: v for k, v in non_null[0].items() if k not in _strip_keys}
            collapsed.update(inner)
            return _normalize(collapsed)
    # Remove const-defaulted fields from required — a field with a const value
    # is always present with that fixed value, so required vs optional is moot.
    if "required" in out and "properties" in out:
        props = out["properties"]
        out["required"] = [
            f for f in out["required"] if not (f in props and isinstance(props[f], dict) and "const" in props[f])
        ]
        if not out["required"]:
            del out["required"]
    # Recursively normalize nested structures
    if "anyOf" in out:
        out["anyOf"] = [_normalize(v) if isinstance(v, dict) else v for v in out["anyOf"]]
    if "properties" in out:
        out["properties"] = {k: _normalize(v) for k, v in out["properties"].items()}
    if "items" in out and isinstance(out["items"], dict):
        out["items"] = _normalize(out["items"])
    return out


if __name__ == "__main__":
    pytest_bazel.main()
