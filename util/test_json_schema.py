from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, create_model
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from util.json_schema import SchemaNameConflictError, register_models

_REF_TEMPLATE = "#/components/schemas/{model}"


class Shared(BaseModel):
    label: str


FirstSameName = create_model("SameName", first_value=(str, ...), __module__="test_json_schema.first")
SecondSameName = create_model("SameName", second_value=(int, ...), __module__="test_json_schema.second")
FirstRoot = create_model("FirstRoot", shared=(Shared, ...), same_name=(FirstSameName, ...))
SecondRoot = create_model("SecondRoot", shared=(Shared, ...), same_name=(SecondSameName, ...))


class Priced(BaseModel):
    amount: Decimal


def _iter_refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value["$ref"]] if "$ref" in value else []
        return own + [ref for child in value.values() for ref in _iter_refs(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in _iter_refs(child)]
    return []


def _resolve(components: dict[str, Any], ref: str) -> Any:
    document = {"components": {"schemas": components}}
    base_uri = "https://json-schema-test.invalid/openapi.json"
    registry = Registry().with_resource(base_uri, Resource.from_contents(document, default_specification=DRAFT202012))
    return registry.resolver(base_uri).lookup(ref).contents


def test_batch_allocation_dedupes_shared_and_separates_same_named_models() -> None:
    components: dict[str, Any] = {}
    register_models(
        components,
        [("FirstRoot", "validation", TypeAdapter(FirstRoot)), ("SecondRoot", "validation", TypeAdapter(SecondRoot))],
        ref_template=_REF_TEMPLATE,
    )

    for ref in _iter_refs(components):
        assert _resolve(components, ref) != {"$ref": ref}
    first_same_name = components["FirstRoot"]["properties"]["same_name"]["$ref"]
    second_same_name = components["SecondRoot"]["properties"]["same_name"]["$ref"]
    assert first_same_name != second_same_name
    assert "first_value" in _resolve(components, first_same_name)["properties"]
    assert "second_value" in _resolve(components, second_same_name)["properties"]
    assert components["FirstRoot"]["properties"]["shared"] == components["SecondRoot"]["properties"]["shared"]
    assert components["Shared"]["properties"]["label"] == {"title": "Label", "type": "string"}


def test_model_root_body_sits_under_its_key_and_alias_keys_ref_it() -> None:
    components: dict[str, Any] = {}
    register_models(
        components,
        [("Shared", "validation", TypeAdapter(Shared)), ("SharedAlias", "validation", TypeAdapter(Shared))],
        ref_template=_REF_TEMPLATE,
    )

    assert components["Shared"]["type"] == "object"
    assert components["SharedAlias"] == {"$ref": "#/components/schemas/Shared"}


def test_existing_component_identical_skips_different_raises() -> None:
    components: dict[str, Any] = {}
    register_models(components, [("Shared", "validation", TypeAdapter(Shared))], ref_template=_REF_TEMPLATE)
    existing = dict(components)

    register_models(components, [("FirstRoot", "validation", TypeAdapter(FirstRoot))], ref_template=_REF_TEMPLATE)
    assert components["Shared"] == existing["Shared"]

    other_shared = create_model("Shared", label=(int, ...), __module__="test_json_schema.other")
    with pytest.raises(SchemaNameConflictError) as excinfo:
        register_models(components, [("Other", "validation", TypeAdapter(other_shared))], ref_template=_REF_TEMPLATE)
    assert excinfo.value.name == "Shared"


def test_permissive_empty_schema_is_registered() -> None:
    components: dict[str, Any] = {}
    register_models(components, [("Anything", "validation", TypeAdapter(Any))], ref_template=_REF_TEMPLATE)
    assert components["Anything"] == {}


def test_mode_is_per_entry() -> None:
    components: dict[str, Any] = {}
    register_models(
        components,
        [("Parsed", "validation", TypeAdapter(Priced)), ("Dumped", "serialization", TypeAdapter(Priced))],
        ref_template=_REF_TEMPLATE,
    )
    # Two modes of one model allocate `Priced-Input`/`Priced-Output`, so each key holds a `$ref` alias.
    parsed = _resolve(components, components["Parsed"]["$ref"])
    dumped = _resolve(components, components["Dumped"]["$ref"])
    assert dumped["properties"]["amount"]["type"] == "string"
    assert parsed["properties"]["amount"] != dumped["properties"]["amount"]


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
