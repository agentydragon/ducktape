"""Register models for wire formats outside FastAPI's route graph."""

from pydantic import BaseModel
from pydantic.json_schema import JsonSchemaMode, JsonSchemaValue, models_json_schema


def add_model_schema(document: JsonSchemaValue, model: type[BaseModel], *, mode: JsonSchemaMode) -> None:
    """Add the model and its referenced definitions without shadowing another component."""
    _, definitions = models_json_schema([(model, mode)], ref_template="#/components/schemas/{model}")
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in definitions["$defs"].items():
        if name in schemas and schemas[name] != schema:
            raise ValueError(f"conflicting OpenAPI component: {name}")
        schemas[name] = schema
