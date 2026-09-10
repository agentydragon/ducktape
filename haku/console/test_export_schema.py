from __future__ import annotations

from typing import Any

import pytest_bazel

from haku.console.export_schema import console_openapi_document


def schemas() -> dict[str, Any]:
    return dict(console_openapi_document()["components"]["schemas"])


def test_tool_call_serializer_preserves_the_structured_frontend_schema() -> None:
    published = schemas()["ToolCallRecord"]
    assert {
        "tool_call_id",
        "caller",
        "status",
        "arguments",
        "rationale",
        "result",
        "decision_note",
        "decision_operator_id",
    } <= published["properties"].keys()
    assert {"tool_call_id", "caller", "status", "arguments"} <= set(published["required"])


if __name__ == "__main__":
    pytest_bazel.main()
