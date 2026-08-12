"""Which `initialize` fields does the CLI take, which does it reject, and which does it ignore?

Three questions, because they have three different answers. Acceptance is nearly unconditional —
an invented field name and a wrong-typed `agents` are both answered `success` — so the sweep's
real finding is the short list of fields that *do* get validated. Structured output is checked
separately because the field is accepted in a shape that does nothing at all.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from haku.cli_protocol.probes.harness import Probe

CAPITAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}


async def handshake(label: str, **fields: Any) -> None:
    probe = Probe()
    await probe.start()
    try:
        response = await probe.control({"subtype": "initialize", **fields}, seconds=60)
        if response["subtype"] == "error":
            print(f"  [{label}] REJECTED: {response['error']}", flush=True)
            return
        commands = response["response"]["commands"]
        print(f"  [{label}] accepted, {len(commands)} commands", flush=True)
    finally:
        await probe.stop()


async def sweep() -> None:
    print("== acceptance ==", flush=True)
    await handshake("baseline")
    await handshake("title", title="a probe session")
    await handshake("skills", skills=["buildbuddy_api"])
    await handshake("supportedDialogKinds", supportedDialogKinds=["refusal_fallback_prompt"])
    await handshake("a dialog kind that does not exist", supportedDialogKinds=["nope"])
    await handshake("a field that does not exist", nonsenseField=True)

    print("== validation ==", flush=True)
    await handshake("skills as a string", skills="not-a-list")
    await handshake("hooks as a list", hooks=["nope"])
    await handshake("agents as a number", agents=42)


async def structured_output(label: str, json_schema: dict[str, Any]) -> None:
    probe = Probe()
    await probe.start()
    try:
        await probe.control({"subtype": "initialize", "jsonSchema": json_schema}, seconds=60)
        await probe.prompt("What is the capital of Japan?")
        result = await probe.wait_for("result", seconds=120)
        print(f"  [{label}] structured_output={json.dumps(result.get('structured_output'))}", flush=True)
        print(f"  [{label}] result={str(result.get('result'))[:120]!r}", flush=True)
    finally:
        await probe.stop()


async def main() -> None:
    await sweep()
    print("== structured output ==", flush=True)
    await structured_output("bare schema", CAPITAL_SCHEMA)
    # The shape `ClaudeAgentOptions`' output-format setting takes. Accepted here, and inert.
    await structured_output("wrapped schema", {"type": "json_schema", "schema": CAPITAL_SCHEMA})


if __name__ == "__main__":
    asyncio.run(main())
