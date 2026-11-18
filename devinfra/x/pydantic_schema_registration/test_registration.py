from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, TypeAdapter, create_model
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from airlock.models import (
    ConnectedOAuthStatus,
    DeploymentInfo,
    DisconnectedOAuthStatus,
    ExpiredOAuthStatus,
    OAuthProviderStatus,
)
from devinfra.x.pydantic_schema_registration.export_schema import build_schema
from devinfra.x.pydantic_schema_registration.schema_models import SharedRecord
from study_casino.state import WsStateChangedMessage


def _iter_refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        refs = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        return refs + [ref for child in value.values() for ref in _iter_refs(child)]
    if isinstance(value, list):
        return [ref for child in value for ref in _iter_refs(child)]
    return []


def _resolve_local_ref(document: dict[str, Any], ref: str) -> Any:
    assert ref.startswith("#/")
    base_uri = "https://schema-registration.invalid/document.json"
    resource = Resource.from_contents(document, default_specification=DRAFT202012)
    resolver = Registry().with_resource(base_uri, resource).resolver(base_uri)
    return resolver.lookup(ref).contents


def _assert_refs_resolve_to_bodies(document: dict[str, Any]) -> None:
    for ref in _iter_refs(document):
        target = _resolve_local_ref(document, ref)
        assert target is not None
        assert target != {"$ref": ref}, f"{ref} points directly to itself"


def _resolve_schema(document: dict[str, Any], schema: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return _resolve_local_ref(document, schema["$ref"])
    return schema


def test_multi_type_allocator_shares_refs_disambiguates_names_and_validates_output() -> None:
    document = build_schema()
    Draft202012Validator.check_schema(document)

    _assert_refs_resolve_to_bodies(document)

    first = _resolve_schema(document, document["properties"]["first"])
    second = _resolve_schema(document, document["properties"]["second"])
    first_same_name_ref = first["properties"]["same_name"]["$ref"]
    second_same_name_ref = second["properties"]["same_name"]["$ref"]
    assert first_same_name_ref != second_same_name_ref
    assert _resolve_local_ref(document, first_same_name_ref)["properties"]["first_value"]["type"] == "string"
    assert _resolve_local_ref(document, second_same_name_ref)["properties"]["second_value"]["type"] == "integer"
    assert first["properties"]["shared"]["$ref"] == second["properties"]["shared"]["$ref"]

    value = {
        "first": {"shared": {"label": "one", "sequence": 1}, "same_name": {"first_value": "one"}},
        "second": {"shared": {"label": "two", "sequence": 2}, "same_name": {"second_value": 2}},
        "validation": {"parsed_json": '{"count": 3}', "amount": 12.34},
        "serialization": {"parsed_json": {"count": 3}, "amount": "12.34"},
    }
    Draft202012Validator(document).validate(value)
    with pytest.raises(ValidationError):
        Draft202012Validator(document).validate(
            {**value, "first": {"shared": {"label": "one", "sequence": 1}, "same_name": {"second_value": 2}}}
        )


def test_allocator_keeps_validation_and_serialization_schemas_distinct() -> None:
    document = build_schema()
    validation = _resolve_schema(document, document["properties"]["validation"])
    serialization = _resolve_schema(document, document["properties"]["serialization"])

    assert validation["properties"]["parsed_json"]["type"] == "string"
    assert serialization["properties"]["parsed_json"]["type"] == "object"
    assert validation["properties"]["amount"] != serialization["properties"]["amount"]
    assert serialization["properties"]["amount"]["type"] == "string"


def test_fastapi_components_merge_and_preexisting_name_collision_behavior() -> None:
    class FastApiResponse(BaseModel):
        existing: SharedRecord

    class AddedModel(BaseModel):
        added: SharedRecord

    app = FastAPI()

    @app.get("/existing", response_model=FastApiResponse)
    def existing() -> FastApiResponse:
        raise NotImplementedError

    document = app.openapi()
    components = document["components"]["schemas"]
    existing_schema = components["FastApiResponse"]
    added_schemas, added_definitions = TypeAdapter.json_schemas(
        [("AddedModel", "validation", TypeAdapter(AddedModel))], ref_template="#/components/schemas/{model}"
    )
    components.update(added_definitions["$defs"])
    added_root = added_schemas[("AddedModel", "validation")]
    assert added_root == {"$ref": "#/components/schemas/AddedModel"}
    assert "AddedModel" in components

    assert components["FastApiResponse"] == existing_schema
    _assert_refs_resolve_to_bodies(document)

    route_schema = document["paths"]["/existing"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    validation_document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"existing": route_schema, "added": added_root},
        "components": {"schemas": components},
    }
    Draft202012Validator.check_schema(validation_document)
    validator = Draft202012Validator(validation_document)
    validator.validate(
        {
            "existing": {"existing": {"label": "from FastAPI", "sequence": 1}},
            "added": {"added": {"label": "from Pydantic", "sequence": 2}},
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "existing": {"existing": {"label": "missing sequence"}},
                "added": {"added": {"label": "from Pydantic", "sequence": 2}},
            }
        )

    conflicting_model = create_model(
        "FastApiResponse", replacement=(str, ...), __module__="pydantic_registration_spike.external"
    )
    conflicting_schemas, conflicting_definitions = TypeAdapter.json_schemas(
        [("FastApiResponse", "validation", TypeAdapter(conflicting_model))], ref_template="#/components/schemas/{model}"
    )
    components.update(conflicting_definitions["$defs"])
    conflicting_root = conflicting_schemas[("FastApiResponse", "validation")]
    assert conflicting_root == {"$ref": "#/components/schemas/FastApiResponse"}
    assert components["FastApiResponse"] != existing_schema
    assert document["paths"]["/existing"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ] == ("#/components/schemas/FastApiResponse")
    _assert_refs_resolve_to_bodies(document)
    conflict_document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"existing": route_schema},
        "components": {"schemas": components},
    }
    Draft202012Validator.check_schema(conflict_document)
    with pytest.raises(ValidationError):
        Draft202012Validator(conflict_document).validate(
            {"existing": {"existing": {"label": "from FastAPI", "sequence": 1}}}
        )


def test_study_casino_websocket_and_airlock_serialization_models() -> None:
    airlock_models = [
        ConnectedOAuthStatus,
        DeploymentInfo,
        DisconnectedOAuthStatus,
        ExpiredOAuthStatus,
        OAuthProviderStatus,
    ]
    inputs: list[tuple[str, Literal["validation", "serialization"], TypeAdapter[Any]]] = [
        (model.__name__, "serialization", TypeAdapter(model)) for model in airlock_models
    ]
    inputs.append(("WsStateChangedMessage", "validation", TypeAdapter(WsStateChangedMessage)))
    schemas, definitions = TypeAdapter.json_schemas(inputs)
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {name: schema for (name, _mode), schema in schemas.items()},
        **definitions,
    }
    _assert_refs_resolve_to_bodies(document)

    websocket_message = WsStateChangedMessage.model_validate({"type": "state_changed"})
    Draft202012Validator(document).validate({"WsStateChangedMessage": websocket_message.model_dump(mode="json")})

    airlock_payloads: list[tuple[str, BaseModel, TypeAdapter[Any]]] = [
        (
            "ConnectedOAuthStatus",
            ConnectedOAuthStatus(expires_at=datetime(2030, 1, 1, tzinfo=UTC), scope="read"),
            TypeAdapter(ConnectedOAuthStatus),
        ),
        ("DeploymentInfo", DeploymentInfo(image_tag="fixture"), TypeAdapter(DeploymentInfo)),
        ("DisconnectedOAuthStatus", DisconnectedOAuthStatus(), TypeAdapter(DisconnectedOAuthStatus)),
        (
            "ExpiredOAuthStatus",
            ExpiredOAuthStatus(expires_at=datetime(2030, 1, 1, tzinfo=UTC), scope="read", last_refresh_error="fixture"),
            TypeAdapter(ExpiredOAuthStatus),
        ),
        (
            "OAuthProviderStatus",
            OAuthProviderStatus(
                name="fixture",
                display_name="Fixture provider",
                provider_type="oauth",
                requested_scopes=["read"],
                status=ConnectedOAuthStatus(expires_at=datetime(2030, 1, 1, tzinfo=UTC), scope="read"),
            ),
            TypeAdapter(OAuthProviderStatus),
        ),
    ]
    for name, payload, adapter in airlock_payloads:
        Draft202012Validator(document).validate({name: adapter.dump_python(payload, mode="json")})


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
