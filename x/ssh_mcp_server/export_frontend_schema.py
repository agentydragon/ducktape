"""Emit the SSH MCP tool schemas as OpenAPI for frontend Zod code generation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastmcp import Client

from x.ssh_mcp_server.server import SshSettings, TargetConfig, build_mcp


async def _exec_schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    # Tool schemas depend on the registered function signatures, not on configured targets.
    # The paths are placeholders; build_mcp does not read them while registering tools.
    settings = SshSettings(
        known_hosts_file=Path("/schema/known_hosts"),
        targets=[TargetConfig(host="schema-host", user="schema-user", identity_file=Path("/schema/key"))],
    )
    async with Client(build_mcp(settings)) as client:
        tool = next((item for item in await client.list_tools() if item.name == "exec"), None)
    if tool is None:
        raise RuntimeError("SSH MCP server did not register the exec tool")
    if tool.outputSchema is None:
        raise RuntimeError("SSH MCP server did not publish an output schema for exec")
    return tool.inputSchema, tool.outputSchema


def _component_schemas(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Move any Pydantic local definitions to OpenAPI components and rewrite their refs."""
    definitions = schema.get("$defs", {})
    components = {
        f"{name}{definition_name}": _rewrite_refs(definition, name)
        for definition_name, definition in definitions.items()
    }
    root_schema = {key: value for key, value in schema.items() if key != "$defs"}
    components[name] = _rewrite_refs(root_schema, name)
    return components


def _rewrite_refs(value: Any, prefix: str) -> Any:
    if isinstance(value, dict):
        rewritten = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/"):
                rewritten[key] = f"#/components/schemas/{prefix}{item.removeprefix('#/$defs/')}"
            else:
                rewritten[key] = _rewrite_refs(item, prefix)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_refs(item, prefix) for item in value]
    return value


def main() -> None:
    input_schema, output_schema = asyncio.run(_exec_schemas())
    schemas = {
        **_component_schemas(input_schema, "SshExecArguments"),
        **_component_schemas(output_schema, "SshExecResult"),
    }
    print(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "SSH MCP tool schemas", "version": "1.0.0"},
                # Keep the components reachable by OpenAPI generators, even though this is only a
                # schema carrier and does not describe a real HTTP endpoint.
                "paths": {
                    "/exec": {
                        "post": {
                            "operationId": "sshExecSchema",
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {"schema": {"$ref": "#/components/schemas/SshExecArguments"}}
                                },
                            },
                            "responses": {
                                "200": {
                                    "description": "SSH exec result",
                                    "content": {
                                        "application/json": {"schema": {"$ref": "#/components/schemas/SshExecResult"}}
                                    },
                                }
                            },
                        }
                    }
                },
                "components": {"schemas": schemas},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
