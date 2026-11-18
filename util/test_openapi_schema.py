from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from pydantic import BaseModel

from util.openapi_schema import register_openapi_model


class Child(BaseModel):
    value: int


class Frame(BaseModel):
    child: Child
    next_frame: Frame | None = None


def test_registers_nested_and_recursive_references() -> None:
    document: dict[str, Any] = {}
    register_openapi_model(document, Frame)
    schemas = document["components"]["schemas"]
    assert schemas["Frame"]["properties"]["child"] == {"$ref": "#/components/schemas/Child"}
    assert {"$ref": "#/components/schemas/Frame"} in schemas["Frame"]["properties"]["next_frame"]["anyOf"]
    assert schemas["Child"]["properties"]["value"]["type"] == "integer"
    child_schema = schemas["Child"]
    register_openapi_model(document, Frame)
    assert schemas["Child"] is child_schema


@pytest.mark.parametrize("name", ["Child", "Frame"])
def test_rejects_conflicts_without_overwriting_components(name: str) -> None:
    document = {"components": {"schemas": {name: {"type": "string"}}}}
    with pytest.raises(ValueError, match=f"Conflicting OpenAPI component schema '{name}'"):
        register_openapi_model(document, Frame)
    assert document["components"]["schemas"] == {name: {"type": "string"}}


if __name__ == "__main__":
    pytest_bazel.main()
