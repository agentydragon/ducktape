"""Schema-shape and runtime-validation tests for the exec MCP tool's input model.

The exec input model wraps ``timeout_ms`` in
``Annotated[TimeoutMs, WithJsonSchema({"type": "integer"})]`` so the Pydantic
runtime validators on ``TimeoutMs`` (``gt=0`` / ``le=MAX_EXEC_TIMEOUT_MS``) keep
firing while the JSON Schema sent to Anthropic stays free of the integer-bound
keys (``exclusiveMinimum`` / ``maximum``) that Anthropic strict tool-use mode
rejects with ``"For 'integer' type, properties exclusiveMinimum, maximum are
not supported"``. These tests pin both halves of that contract.
"""

from pathlib import Path

import pytest
import pytest_bazel
from pydantic import ValidationError

from mcp_infra.exec.docker.server import _make_exec_input_model
from mcp_infra.exec.docker.types import AlwaysSetTo
from mcp_infra.exec.models import MAX_EXEC_TIMEOUT_MS


@pytest.fixture
def exec_input_cls() -> type:
    return _make_exec_input_model(allow_user=False, allow_env=False, cwd_policy=AlwaysSetTo(value=Path("/work")))


def test_timeout_ms_schema_omits_integer_bounds(exec_input_cls: type) -> None:
    """Anthropic strict mode rejects exclusiveMinimum/maximum on integer types."""
    schema = exec_input_cls.model_json_schema()
    timeout_schema = schema["properties"]["timeout_ms"]
    assert timeout_schema["type"] == "integer"
    for forbidden in ("exclusiveMinimum", "exclusiveMaximum", "minimum", "maximum"):
        assert forbidden not in timeout_schema, f"{forbidden} leaks into the strict-mode tool schema: {timeout_schema}"


def test_timeout_ms_schema_is_strict_mode_compatible(exec_input_cls: type) -> None:
    """The full input schema must be strict-mode-compatible for Anthropic strict tool use."""
    schema = exec_input_cls.model_json_schema()
    assert schema.get("additionalProperties") is False
    # Both required fields land in `required` (OpenAIStrictModeBaseModel's contract).
    assert set(schema["required"]) >= {"cmd", "timeout_ms"}


def test_timeout_ms_runtime_rejects_zero(exec_input_cls: type) -> None:
    """gt=0 still fires at validation time even though it's hidden from the schema."""
    with pytest.raises(ValidationError):
        exec_input_cls(cmd=["true"], timeout_ms=0)


def test_timeout_ms_runtime_rejects_above_max(exec_input_cls: type) -> None:
    """le=MAX_EXEC_TIMEOUT_MS still fires at validation time."""
    with pytest.raises(ValidationError):
        exec_input_cls(cmd=["true"], timeout_ms=MAX_EXEC_TIMEOUT_MS + 1)


def test_timeout_ms_runtime_accepts_in_range(exec_input_cls: type) -> None:
    instance = exec_input_cls(cmd=["true"], timeout_ms=1000)
    assert instance.timeout_ms == 1000


if __name__ == "__main__":
    pytest_bazel.main()
