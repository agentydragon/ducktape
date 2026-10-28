from __future__ import annotations

import json

import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from jsonschema import Draft202012Validator, ValidationError

from devinfra.x.schema_codegen.infrastructure.discovery.fixture import item_search_spec
from util.bazel.runfiles import get_required_path
from x.google_mcp_server import google_discovery
from x.google_mcp_server.google_discovery import build_generated_tools


async def test_fastmcp_advertises_validated_discovery_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    discovery_doc = json.loads(
        get_required_path("_main/devinfra/x/schema_codegen/infrastructure/discovery/sample_discovery.json").read_text()
    )
    monkeypatch.setattr(google_discovery, "_load_doc", lambda _api_version: discovery_doc)

    server = FastMCP("discovery-schema-spike")
    for tool in build_generated_tools([item_search_spec()], service=None):
        server.add_tool(tool)

    async with Client(server) as client:
        [tool] = await client.list_tools()

    schema = tool.inputSchema
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"query": "small", "labels": ["blue"], "pageSize": 2})
    for invalid_arguments in (
        {"query": "small", "labels": ["blue"]},
        {"query": "small", "labels": ["blue"], "pageSize": "2"},
        {"query": "small", "labels": ["blue"], "pageSize": 2, "extra": True},
    ):
        with pytest.raises(ValidationError):
            validator.validate(invalid_arguments)
    assert schema["properties"]["labels"]["items"]["type"] == "string"
    assert schema["required"] == ["pageSize"]

    output_schema = tool.outputSchema
    assert output_schema is not None
    Draft202012Validator.check_schema(output_schema)
    output_validator = Draft202012Validator(output_schema)
    output_validator.validate({"nextPageToken": "next", "totalItems": 3})
    with pytest.raises(ValidationError):
        output_validator.validate({"nextPageToken": 4})


if __name__ == "__main__":
    pytest_bazel.main()
