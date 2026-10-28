"""Write a test-only serialized exec call/result from the registered FastMCP tool."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fastmcp import Client

from ssh_mcp_server import server as ssh_server
from ssh_mcp_server.server import SshSettings, TargetConfig, build_mcp


async def _producer_payload() -> dict[str, Any]:
    with TemporaryDirectory(prefix="ssh-mcp-fixture-") as temp_dir:
        temp_path = Path(temp_dir)
        known_hosts = temp_path / "known_hosts"
        known_hosts.write_text("fixture.example ssh-ed25519 AAAA\n", encoding="utf-8")
        identity_file = temp_path / "identity"
        identity_file.write_text("not-a-real-key\n", encoding="utf-8")
        settings = SshSettings(
            known_hosts_file=known_hosts,
            targets=[TargetConfig(host="fixture.example", user="runner", identity_file=identity_file)],
            command_timeout_seconds=30,
        )
        call_arguments = json.loads(
            json.dumps(
                {
                    "host": "fixture.example",
                    "user": "runner",
                    "command": "printf producer-fixture",
                    "timeout_seconds": 5,
                }
            )
        )
        with patch.object(ssh_server, "_run_ssh", return_value=(("producer-fixture\n", False), ("", False), 0)):
            async with Client(build_mcp(settings)) as client:
                result = await client.call_tool("exec", call_arguments)
            serialized_result = json.loads(result.content[0].text)

    return {"arguments": call_arguments, "result": serialized_result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(asyncio.run(_producer_payload()), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
