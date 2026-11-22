"""Register Pydantic models that are absent from the HTTP route graph."""

from typing import Any

from pydantic import BaseModel

_COMPONENT_REF = "#/components/schemas/{model}"


def register_openapi_model(document: dict[str, Any], model: type[BaseModel]) -> None:
    """Add a model's validation schema and definitions, rejecting incompatible names."""
    root = model.model_json_schema(ref_template=_COMPONENT_REF)
    definitions = root.pop("$defs", {})
    name = model.__name__
    if root != {"$ref": _COMPONENT_REF.format(model=name)}:
        if name in definitions and definitions[name] != root:
            raise ValueError(f"Conflicting OpenAPI component schema {name!r}")
        definitions[name] = root

    components = document.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in definitions.items():
        if name in components and components[name] != schema:
            raise ValueError(f"Conflicting OpenAPI component schema {name!r}")
    components.update({name: schema for name, schema in definitions.items() if name not in components})
