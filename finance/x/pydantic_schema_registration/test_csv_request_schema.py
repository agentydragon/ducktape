from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import TypeAdapter
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from finance.budget.wire import BudgetSummaryCsvRequest


def _iter_refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        refs = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        return refs + [ref for child in value.values() for ref in _iter_refs(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in _iter_refs(child)]
    return []


def test_csv_request_validation_schema_accepts_tagged_request() -> None:
    adapter = TypeAdapter(BudgetSummaryCsvRequest)
    schemas, definitions = TypeAdapter.json_schemas(
        [("BudgetSummaryCsvRequest", "validation", adapter)], ref_template="#/$defs/{model}"
    )
    root = schemas[("BudgetSummaryCsvRequest", "validation")]
    schema_document = {**root, "$schema": "https://json-schema.org/draft/2020-12/schema", **definitions}
    base_uri = "https://schema-registration.invalid/csv-request.json"
    resolver = (
        Registry()
        .with_resource(base_uri, Resource.from_contents(schema_document, default_specification=DRAFT202012))
        .resolver(base_uri)
    )
    for ref in _iter_refs(schema_document):
        assert resolver.lookup(ref).contents is not None
    Draft202012Validator.check_schema(schema_document)
    validator = Draft202012Validator(schema_document)
    request = {
        "window": {"kind": "trailing_months", "months": 12},
        "adjustments": {"transaction-a": {"kind": "override", "monthly": "23.45"}, "transaction-b": {"kind": "hidden"}},
    }

    validator.validate(request)
    model = adapter.validate_python(request)
    assert model.adjustments["transaction-a"].model_dump(mode="json") == {"kind": "override", "monthly": "23.45"}
    validator.validate(model.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        validator.validate({**request, "window": {"kind": "trailing_months", "months": 0}})


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
