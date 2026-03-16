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

# (model_name, model_class, event_name_or_None)
# Models with hookSpecificOutput have event_name; common-only models have None.
_ALL_OUTPUT_MODELS: list[tuple[str, type, str | None]] = [
    ("SessionStart", SessionStartOutput, "SessionStart"),
    ("Setup", SetupOutput, "Setup"),
    ("PreToolUse", PreToolUseOutput, "PreToolUse"),
    ("PostToolUse", PostToolUseOutput, "PostToolUse"),
    ("PostToolUseFailure", PostToolUseFailureOutput, "PostToolUseFailure"),
    ("UserPromptSubmit", UserPromptSubmitOutput, "UserPromptSubmit"),
    ("Notification", NotificationOutput, "Notification"),
    ("PermissionRequest", PermissionRequestOutput, "PermissionRequest"),
    ("Elicitation", ElicitationOutput, "Elicitation"),
    ("ElicitationResult", ElicitationResultOutput, "ElicitationResult"),
    ("SubagentStart", SubagentStartOutput, "SubagentStart"),
    ("Stop", StopOutput, None),
    ("SubagentStop", SubagentStopOutput, None),
    ("ConfigChange", ConfigChangeOutput, None),
]


def _load_zod_json_schema() -> dict[str, Any]:
    """Load the Zod-derived JSON Schema for hookOutput from runfiles."""
    path = get_required_path("_main/devinfra/claude/claude_api/hooks/schemas/2.1.76/hook_output.json")
    return json.loads(path.read_text())  # type: ignore[no-any-return]


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


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a JSON Schema field for comparison.

    Strips metadata and structural differences that don't affect wire compatibility:
    - Strip metadata keys: title, description, default, discriminator, $defs
    - Collapse nullable anyOf: {"anyOf": [T, {"type": "null"}]} -> T
    - Normalize Record<string, unknown>: additionalProperties:{} = true
    - Unify oneOf -> anyOf (Pydantic uses oneOf for discriminated unions, Zod uses anyOf)
    - Normalize required: remove const-defaulted fields (always have a fixed value)
    - Recurse into nested structures (properties, items, anyOf/oneOf variants)
    """
    _strip_keys = {"title", "description", "default", "discriminator", "$defs"}
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


@pytest.fixture(scope="module")
def zod_schema() -> dict[str, Any]:
    """Load the Zod JSON Schema once per test module."""
    return _load_zod_json_schema()


def test_zod_json_schema_loads(zod_schema: dict[str, Any]) -> None:
    """Smoke test: the generated JSON Schema file loads and has expected structure."""
    assert "properties" in zod_schema, f"Expected 'properties' in schema, got keys: {list(zod_schema.keys())}"
    props = zod_schema["properties"]
    assert "continue" in props, f"Expected 'continue' field, got: {list(props.keys())}"
    assert "hookSpecificOutput" in props, f"Expected 'hookSpecificOutput' field, got: {list(props.keys())}"


@pytest.mark.parametrize(
    ("model_name", "model_class", "event_name"),
    [(name, cls, ev) for name, cls, ev in _ALL_OUTPUT_MODELS],
    ids=[name for name, _, _ in _ALL_OUTPUT_MODELS],
)
def test_output_model_matches_zod(
    zod_schema: dict[str, Any], model_name: str, model_class: type, event_name: str | None
) -> None:
    """Verify a single Pydantic output model matches the Zod hookOutput schema."""
    zod_composed = _compose_zod_model_schema(zod_schema, event_name)
    pydantic_schema = TypeAdapter(model_class).json_schema(mode="serialization")

    zod_normalized = _normalize(zod_composed)
    pydantic_normalized = _normalize(inline_refs(pydantic_schema))

    if zod_normalized != pydantic_normalized:
        zod_text = json.dumps(zod_normalized, indent=2, sort_keys=True)
        pyd_text = json.dumps(pydantic_normalized, indent=2, sort_keys=True)
        diff = "\n".join(
            difflib.unified_diff(
                zod_text.splitlines(),
                pyd_text.splitlines(),
                fromfile=f"Zod ({model_name})",
                tofile=f"Pydantic ({model_name})",
                lineterm="",
                n=3,
            )
        )
        pytest.fail(f"Schema mismatch for {model_name}:\n{diff}")


def test_all_zod_variants_covered(zod_schema: dict[str, Any]) -> None:
    """Every Zod hookSpecificOutput variant must have a Pydantic model in _ALL_OUTPUT_MODELS."""
    hso = zod_schema["properties"]["hookSpecificOutput"]
    zod_event_names: set[str] = set()
    for variant in hso.get("anyOf", hso.get("oneOf", [])):
        hen = variant.get("properties", {}).get("hookEventName", {})
        name = hen.get("const")
        if name:
            zod_event_names.add(name)

    tested_event_names = {ev for _, _, ev in _ALL_OUTPUT_MODELS if ev is not None}
    missing = zod_event_names - tested_event_names
    assert not missing, f"Zod variants without Pydantic models: {missing}"


if __name__ == "__main__":
    pytest_bazel.main()
