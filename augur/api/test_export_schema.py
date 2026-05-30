"""Test that export_schema emits a valid OpenAPI doc from its own-repo fixture.

This exercises the repo-agnostic fixture lookup the frontend Zod codegen relies on
(`get_required_own_repo_path("augur/api/testdata/config.yaml")`): it must resolve
the `data`-dep fixture and build the real app end to end. Running as the ducktape
main repo proves the `_main` runfiles prefix still resolves after the migration off
the previously-hardcoded prefix.
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
    assert doc["paths"], "expected the fixture app to register routes"
    assert doc["components"]["schemas"], "expected component schemas for Zod codegen"
    # The calibration routes are the reason the fixture builds the full app; their
    # presence confirms the fixture config (and its calibration catalog data dep)
    # resolved, not a degenerate empty app.
    assert any(path.startswith("/api/calibration/") for path in doc["paths"])


if __name__ == "__main__":
    pytest_bazel.main()
