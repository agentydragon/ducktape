"""Can the client be the MCP server, serving JSON-RPC back over the control channel?

If it can, Haku gains tools with no second process, no port and no credential on the wire — the
implementation stays in the console, which already holds whatever the tool needs. The probe
serves one tool returning a word no model would guess, so the word appearing in the final answer
is proof the whole round trip ran rather than that the model played along.
"""

from __future__ import annotations

import asyncio
from typing import Any

from haku.cli_protocol.probes.harness import Probe, allow_every_tool

SERVER = "probe"
SECRET = "PLATYPUS"
TOOL = {
    "name": "haku_secret_word",
    "description": "Returns the secret word of the day.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}


def _serve(message: dict[str, Any]) -> dict[str, Any]:
    match message.get("method"):
        case "initialize":
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER, "version": "0.1.0"},
            }
        case "tools/list":
            return {"tools": [TOOL]}
        case "tools/call":
            return {"content": [{"type": "text", "text": SECRET}], "isError": False}
        case _:
            # Notifications carry no `id` and still arrive as control requests, so they still
            # need an answer; an empty result is the convention.
            return {}


async def main() -> None:
    probe = Probe("--permission-prompt-tool", "stdio")
    await probe.start()

    async def inbound(frame: dict[str, Any]) -> dict[str, Any] | None:
        request = frame.get("request") or {}
        if request.get("subtype") != "mcp_message":
            return await allow_every_tool(frame)
        message = request["message"]
        print(f"  mcp {request['server_name']} {message.get('method')}", flush=True)
        return {"mcp_response": {"jsonrpc": "2.0", "id": message.get("id"), "result": _serve(message)}}

    probe.inbound = inbound
    await probe.control({"subtype": "initialize", "sdkMcpServers": [SERVER]})

    await probe.prompt("Call the haku_secret_word tool and tell me exactly what it returned.")
    result = await probe.wait_for("result", seconds=150)
    answer = str(result.get("result"))
    print(f"  the model saw the tool's result: {SECRET in answer}", flush=True)
    print(f"  result: {answer[:200]!r}", flush=True)
    await probe.stop()


if __name__ == "__main__":
    asyncio.run(main())
