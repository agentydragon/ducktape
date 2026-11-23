"""Compare the published Pydantic hook output with the pinned Claude Code Zod schema.

Compare the synchronous output contract after accounting for representation differences:
metadata, optional fields serialized with nulls omitted, and literal discriminators that
Pydantic supplies by default. The async output envelope has a separate Zod schema.
"""

import difflib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from python.runfiles import Runfiles

from devinfra.claude.claude_api.hooks.output import HookOutput
from devinfra.claude.claude_api.json_schema import inline_refs

_METADATA_KEYS = {"title", "description", "default", "discriminator", "$defs", "$schema"}


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in schema.items() if key not in _METADATA_KEYS}
    if out.get("additionalProperties") in ({}, True):
        out["additionalProperties"] = True
        if out.get("propertyNames") == {"type": "string"}:
            del out["propertyNames"]
    if "oneOf" in out and "anyOf" not in out:
        # Pydantic's discriminated unions and Zod's unions describe disjoint alternatives.
        out["anyOf"] = out.pop("oneOf")
    if "anyOf" in out:
        alternatives = out["anyOf"]
        non_null = [branch for branch in alternatives if branch != {"type": "null"}]
        if len(alternatives) == 2 and len(non_null) == 1 and isinstance(non_null[0], dict):
            del out["anyOf"]
            return _normalize(out | non_null[0])
        out["anyOf"] = [_normalize(branch) if isinstance(branch, dict) else branch for branch in alternatives]
    if "properties" in out:
        properties = out["properties"]
        if "required" in out:
            # Only the existing wire discriminators may differ in requiredness.
            defaulted_discriminators = {
                name for name in ("hookEventName", "behavior", "type") if "const" in properties.get(name, {})
            }
            required = sorted(set(out.pop("required")) - defaulted_discriminators)
            if required:
                out["required"] = required
        out["properties"] = {name: _normalize(field) for name, field in properties.items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _normalize(out["items"])
    return out


def _comparison_shape(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(inline_refs(schema))
    specific_output = normalized.get("properties", {}).get("hookSpecificOutput", {})
    if "anyOf" in specific_output:
        # Preserve every branch, including duplicates, regardless of producer ordering.
        specific_output["anyOf"].sort(key=lambda branch: json.dumps(branch, sort_keys=True))
    return normalized


def test_hook_output_matches_zod() -> None:
    schema_location = os.environ["HOOK_OUTPUT_SCHEMA"]
    schema_path = Runfiles.CreateOrRaise().Rlocation(schema_location)
    assert schema_path is not None, schema_location
    zod = _comparison_shape(json.loads(Path(schema_path).read_text()))
    pydantic = _comparison_shape(HookOutput.model_json_schema(mode="serialization"))

    if zod != pydantic:
        diff = "\n".join(
            difflib.unified_diff(
                json.dumps(zod, indent=2, sort_keys=True).splitlines(),
                json.dumps(pydantic, indent=2, sort_keys=True).splitlines(),
                fromfile="Claude Code Zod",
                tofile="Pydantic",
                lineterm="",
            )
        )
        pytest.fail(f"Hook output schema mismatch:\n{diff}")


def test_missing_local_definition_reports_its_reference() -> None:
    schema = {"properties": {"value": {"$ref": "#/$defs/Missing"}}}
    with pytest.raises(ValueError, match=r"#/\$defs/Missing"):
        inline_refs(schema)
