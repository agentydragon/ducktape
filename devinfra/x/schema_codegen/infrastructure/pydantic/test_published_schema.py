from __future__ import annotations

import pytest_bazel
from jsonschema import Draft202012Validator

from devinfra.x.schema_codegen.infrastructure.pydantic.models import SearchRequest
from openai_utils.json_schema import openai_json_schema
from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema


def test_pydantic_publication_is_valid_json_schema() -> None:
    schema = openai_json_schema(SearchRequest)
    validate_openai_strict_mode_schema(schema, "SearchRequest")
    search_schema = schema["properties"]["search"]
    assert "anyOf" in search_schema
    assert "oneOf" not in search_schema
    assert "note" in schema["required"]

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"search": {"kind": "thread", "max_results": None}, "note": None})
    validator.validate({"search": {"kind": "email", "recipient": "agent@example.test"}, "note": "draft"})
    for invalid_arguments in (
        {"search": {"kind": "thread", "max_results": None}},
        {"search": {"kind": "thread", "max_results": "many"}, "note": None},
        {"search": {"kind": "other", "recipient": "x"}, "note": None},
    ):
        assert not validator.is_valid(invalid_arguments)


if __name__ == "__main__":
    pytest_bazel.main()
