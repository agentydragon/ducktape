from __future__ import annotations

import pytest
import pytest_bazel
from jsonschema import Draft202012Validator, ValidationError

from loom.gym.baseline_llm import question_schema
from loom.gym.scoring import QUANTILE_LEVELS
from loom.gym.task import ScalarQuestion


def _contains_reference(node: object) -> bool:
    if isinstance(node, dict):
        return "$ref" in node or any(_contains_reference(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_reference(value) for value in node)
    return False


def test_question_schema_inlines_nested_model_for_provider_use() -> None:
    schema = question_schema(ScalarQuestion(text="What will the value be?", unit="units"))
    Draft202012Validator.check_schema(schema)
    assert "$defs" not in schema
    assert not _contains_reference(schema)

    validator = Draft202012Validator(schema)
    validator.validate({"quantiles": {str(level): float(index) for index, level in enumerate(QUANTILE_LEVELS)}})
    with pytest.raises(ValidationError):
        validator.validate({"quantiles": {str(level): 1.0 for level in QUANTILE_LEVELS[:-1]}})
    with pytest.raises(ValidationError):
        validator.validate({"quantiles": {str(level): "1" for level in QUANTILE_LEVELS}})


if __name__ == "__main__":
    pytest_bazel.main()
