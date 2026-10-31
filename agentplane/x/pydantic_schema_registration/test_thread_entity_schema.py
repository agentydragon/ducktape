from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import TypeAdapter
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from agentplane.history_service.fold.views import ThreadEntityView


def _iter_refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        refs = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        return refs + [ref for child in value.values() for ref in _iter_refs(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in _iter_refs(child)]
    return []


def test_stream_entity_validation_schema_accepts_a_serialized_row() -> None:
    adapter: TypeAdapter[ThreadEntityView] = TypeAdapter(ThreadEntityView)
    schemas, definitions = TypeAdapter.json_schemas(
        [("ThreadEntityView", "validation", adapter)], ref_template="#/$defs/{model}"
    )
    root = schemas[("ThreadEntityView", "validation")]
    schema_document = {**root, "$schema": "https://json-schema.org/draft/2020-12/schema", **definitions}
    base_uri = "https://schema-registration.invalid/thread-entity.json"
    resolver = (
        Registry()
        .with_resource(base_uri, Resource.from_contents(schema_document, default_specification=DRAFT202012))
        .resolver(base_uri)
    )
    for ref in _iter_refs(schema_document):
        assert resolver.lookup(ref).contents is not None
    Draft202012Validator.check_schema(schema_document)
    validator = Draft202012Validator(schema_document)
    row = {
        "thread_id": "00000000-0000-0000-0000-000000000001",
        "projection_epoch": "epoch-1",
        "entity_id": "item-1",
        "cursor": 1,
        "revision_cursor": 1,
        "pending": False,
        "turn_id": None,
        "text_ref": None,
        "arguments_ref": None,
        "output_ref": None,
        "input_ref": None,
        "entity_kind": "item",
        "state": {
            "kind": 1,
            "tool_name": "search",
            "completion": "finished",
            "tool_succeeded": True,
            "recovery": None,
            "recovery_reason": None,
        },
    }

    validator.validate(row)
    model = adapter.validate_python(row)
    validator.validate(adapter.dump_python(model, mode="json"))
    with pytest.raises(ValidationError):
        validator.validate({**row, "entity_kind": "unknown"})


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
