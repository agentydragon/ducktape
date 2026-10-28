"""Emit the SSH MCP tool input and output schemas as standalone JSON Schema files."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from fastmcp import Client

from ssh_mcp_server.server import SshSettings, TargetConfig, build_mcp


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
    if tool.output_schema is None:
        raise RuntimeError("SSH MCP server did not publish an output schema for exec")
    # Stable titles preserve the existing generated TypeScript names while both artifacts remain
    # the exact standalone schemas FastMCP advertises (with only root metadata normalized).
    input_schema = {**tool.input_schema, "title": "SshExecArguments"}
    output_schema = {**tool.output_schema, "title": "SshExecResult"}
    return input_schema, output_schema


def _write_schema(path: Path, schema: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("arguments_schema", type=Path)
    parser.add_argument("result_schema", type=Path)
    args = parser.parse_args()

    input_schema, output_schema = asyncio.run(_exec_schemas())
    _write_schema(args.arguments_schema, input_schema)
    _write_schema(args.result_schema, output_schema)


if __name__ == "__main__":
    main()
