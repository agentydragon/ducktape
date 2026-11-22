import pytest
import pytest_bazel
from pydantic import BaseModel, computed_field

from util.openapi import add_model_schema


class Item(BaseModel):
    count: int

    @computed_field
    @property
    def doubled(self) -> int:
        return 2 * self.count


class Frame(BaseModel):
    item: Item


def test_nested_frame_points_to_the_emitted_output_component() -> None:
    document: dict = {}
    add_model_schema(document, Frame, mode="serialization")
    schemas = document["components"]["schemas"]
    assert schemas["Frame"]["properties"]["item"]["$ref"] == "#/components/schemas/Item"
    assert "doubled" in schemas["Item"]["properties"]
    add_model_schema(document, Item, mode="serialization")
    with pytest.raises(ValueError, match="conflicting OpenAPI component: Item"):
        add_model_schema(document, Item, mode="validation")


if __name__ == "__main__":
    pytest_bazel.main()
