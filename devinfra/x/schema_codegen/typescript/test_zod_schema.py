from __future__ import annotations

import json

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from util.bazel.runfiles import get_required_path


def test_zod_schema_is_draft_2020_12_and_validates_wire_values() -> None:
    schema = json.loads(get_required_path("_main/devinfra/x/schema_codegen/typescript/zod.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(
        {
            "requestId": "req-1",
            "submittedAt": "2026-10-27T09:30:00-07:00",
            "tags": {"source": "test"},
            "outcome": {"kind": "accepted", "sequence": 0},
            "nullableComment": None,
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "requestId": "req-1",
                "submittedAt": "2026-10-27T09:30:00",
                "tags": {},
                "outcome": {"kind": "accepted", "sequence": 0},
                "nullableComment": None,
            }
        )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "requestId": "req-1",
                "submittedAt": "2026-10-27T09:30:00-07:00",
                "tags": {},
                "outcome": {"kind": "unknown", "reason": "invalid"},
                "nullableComment": None,
            }
        )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "requestId": "req-1",
                "submittedAt": "2026-10-27T09:30:00-07:00",
                "tags": {},
                "outcome": {"kind": "accepted", "sequence": 0},
                "nullableComment": None,
                "extra": "rejected",
            }
        )


if __name__ == "__main__":
    pytest_bazel.main()
