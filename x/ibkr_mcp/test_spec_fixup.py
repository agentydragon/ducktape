"""Unit tests for the Swagger 2.0 → OpenAPI 3.1 transcode helpers, plus a
structural check on the real generated spec.
"""

from __future__ import annotations

import json

import pytest_bazel

from util.bazel.runfiles import get_required_path
from x.ibkr_mcp.route_policy import READ_ONLY_OPERATIONS
from x.ibkr_mcp.spec_fixup import _convert_operation, _convert_parameter, _reachable_definitions, _rewrite_refs


def test_rewrite_refs_repoints_definitions() -> None:
    assert _rewrite_refs({"$ref": "#/definitions/Contract"}) == {"$ref": "#/components/schemas/Contract"}


def test_rewrite_refs_drops_empty_enums() -> None:
    assert _rewrite_refs({"type": "string", "enum": []}) == {"type": "string"}
    assert _rewrite_refs({"enum": ["a", "b"]}) == {"enum": ["a", "b"]}


def test_convert_parameter_wraps_type_in_schema() -> None:
    converted = _convert_parameter({"name": "conids", "in": "query", "required": True, "type": "string"})
    assert converted == {"name": "conids", "in": "query", "required": True, "schema": {"type": "string"}}


def test_convert_parameter_defaults_path_params_required() -> None:
    converted = _convert_parameter({"name": "conid", "in": "path", "type": "integer"})
    assert converted["required"] is True


def test_convert_operation_body_param_becomes_request_body() -> None:
    op = {
        "summary": "Search",
        "parameters": [{"in": "body", "name": "body", "required": True, "schema": {"$ref": "#/definitions/SearchReq"}}],
        "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/SearchResp"}}},
    }
    converted = _convert_operation("POST", "/iserver/secdef/search", op)
    assert converted["operationId"] == "secdef_search"
    assert "parameters" not in converted
    assert converted["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchReq"
    }
    assert converted["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchResp"
    }


def test_reachable_definitions_is_transitive() -> None:
    definitions = {
        "A": {"properties": {"b": {"$ref": "#/definitions/B"}}},
        "B": {"properties": {"c": {"$ref": "#/definitions/C"}}},
        "C": {"type": "string"},
        "Unused": {"type": "string"},
    }
    assert _reachable_definitions({"A"}, definitions) == {"A", "B", "C"}


def test_generated_spec_is_valid_openapi_31() -> None:
    spec = json.loads(get_required_path("_main/x/ibkr_mcp/ibkr.openapi.fixed.json").read_text())
    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) == {path for _, path in READ_ONLY_OPERATIONS}
    # No leftover Swagger 2.0 definition refs anywhere in the document.
    assert "#/definitions/" not in json.dumps(spec)
    # secdef_search's body parameter survived as a requestBody.
    assert "requestBody" in spec["paths"]["/iserver/secdef/search"]["post"]


if __name__ == "__main__":
    pytest_bazel.main()
