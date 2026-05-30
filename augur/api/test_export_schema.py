"""Test that export_schema emits a valid OpenAPI doc for the frontend Zod/TS codegen.

`export_schema.main()` builds the real FastAPI app from an in-Python `Config` (no YAML /
runfiles fixture) and dumps `.openapi()`. This asserts the emitted document is well-formed
and carries the routes + component schemas the frontend consumes — proving the in-Python
deployment registers the full app end to end.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest_bazel

from augur.api import export_schema


def test_export_schema_emits_openapi_with_components() -> None:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        export_schema.main()
    doc = json.loads(buffer.getvalue())

    # A well-formed OpenAPI document with the component schemas the frontend's
    # Zod/TS codegen consumes.
    assert doc["openapi"].startswith("3.")
    assert doc["paths"], "expected the app to register routes"
    assert doc["components"]["schemas"], "expected component schemas for Zod codegen"
    # The calibration routes are registered unconditionally; their presence confirms the
    # in-Python config built the full app, not a degenerate empty one.
    assert any(path.startswith("/api/calibration/") for path in doc["paths"])


if __name__ == "__main__":
    pytest_bazel.main()
