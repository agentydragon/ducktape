"""Do hooks registered over `initialize` fire, and is the client's answer honoured?

The interesting half is the second one. A hook that only observes would be a status feed; a hook
whose `deny` actually stops the tool is a policy seam, and it sits *before* the permission check —
so a host can refuse a call without ever being asked to approve it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from haku.cli_protocol.probes.harness import Probe, allow_every_tool

PRE_TOOL_USE = "cb_pretool"
DENIAL = "the probe's hook says no"


async def main() -> None:
    probe = Probe("--permission-prompt-tool", "stdio")
    await probe.start()

    async def inbound(frame: dict[str, Any]) -> dict[str, Any] | None:
        request = frame.get("request") or {}
        if request.get("subtype") != "hook_callback":
            return await allow_every_tool(frame)
        callback_id = request.get("callback_id")
        print(f"  hook {callback_id}: {json.dumps(request.get('input'))[:300]}", flush=True)
        if callback_id != PRE_TOOL_USE:
            return {}
        return {
            "decision": "block",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENIAL,
            },
        }

    probe.inbound = inbound
    await probe.control(
        {
            "subtype": "initialize",
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hookCallbackIds": [PRE_TOOL_USE]}],
                "UserPromptSubmit": [{"hookCallbackIds": ["cb_prompt"]}],
                "SessionStart": [{"hookCallbackIds": ["cb_session"]}],
            },
        }
    )

    await probe.prompt("Run `echo hello` with the Bash tool, then tell me what it printed.")
    result = await probe.wait_for("result", seconds=120)

    fired = sorted(
        {
            str((frame.get("request") or {}).get("callback_id"))
            for frame in probe.of_type("control_request")
            if (frame.get("request") or {}).get("subtype") == "hook_callback"
        }
    )
    print(f"  hooks fired: {fired}", flush=True)
    print(f"  permission prompt reached the client: {'can_use_tool' in probe.inbound_subtypes()}", flush=True)
    print(f"  denial text reached the model: {DENIAL in json.dumps(probe.frames)}", flush=True)
    print(f"  result: {str(result.get('result'))[:200]!r}", flush=True)
    await probe.stop()


if __name__ == "__main__":
    asyncio.run(main())
